"""Common utilities: config loading, NETCONF connection, target resolution, parallel execution."""

from concurrent import futures
from jnpr.junos import Device
from jnpr.junos.exception import (
    ConnectAuthError,
    ConnectClosedError,
    ConnectError,
    ConnectRefusedError,
    ConnectTimeoutError,
    ConnectUnknownHostError,
)
import configparser
import os
import sys
import threading
from logging import getLogger

logger = getLogger(__name__)

config = None
config_lock = threading.Lock()
args = None

DEFAULT_CONFIG = "config.ini"


def get_default_config():
    """Search for config file in standard locations."""
    # カレントディレクトリ
    if os.path.isfile(DEFAULT_CONFIG):
        return DEFAULT_CONFIG
    # XDG_CONFIG_HOME（未設定なら ~/.config）
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    xdg_path = os.path.join(xdg, "junos-ops", DEFAULT_CONFIG)
    if os.path.isfile(xdg_path):
        return xdg_path
    return DEFAULT_CONFIG


def read_config() -> dict:
    """Read and parse the INI config file.

    :return: dict with keys:

        - ``ok`` (bool): True if config was read and contained at least one
          section.
        - ``path`` (str): the config file path that was read.
        - ``sections`` (list[str]): section names (host identifiers).
        - ``error`` (str | None): human-readable error message if ``ok`` is
          False, else None.

    Side effect: populates the module-level :data:`config` with the parsed
    ``configparser.ConfigParser`` instance. Does not print.
    """
    global config
    config = configparser.ConfigParser(allow_no_value=True)
    config.read(args.config)
    sections = config.sections()
    if len(sections) == 0:
        return {
            "ok": False,
            "path": args.config,
            "sections": [],
            "error": f"{args.config} is empty",
        }
    for section in sections:
        if config.has_option(section, "host"):
            host = config.get(section, "host")
        else:
            host = None
        if host is None:
            # host is [section] name
            config.set(section, "host", section)
        for key in config[section]:
            logger.debug(f"{section} > {key} : {config[section][key]}")
    return {
        "ok": True,
        "path": args.config,
        "sections": list(sections),
        "error": None,
    }


def connect(
    hostname: str, *, gather_facts: bool = True, auto_probe: int = 0
) -> dict:
    """Open a NETCONF connection to a device.

    :param hostname: config section name (host identifier).
    :param gather_facts: when False, ``Device.open()`` skips PyEZ's
        automatic facts collection (~10 RPCs covering chassis
        inventory, routing-engine info, hosts.junos, etc.). Use this
        for fast reachability-only probes such as ``check --connect``;
        callers that genuinely need ``dev.facts`` (most subcommands)
        should leave the default True.
    :param auto_probe: when > 0, do a TCP-level reachability probe
        with this timeout (seconds) before attempting NETCONF/SSH
        negotiation. Lets unreachable hosts fail fast instead of
        hanging on the OS-default TCP connect timeout (60–120 s).
        Default 0 keeps the existing PyEZ behaviour.
    :return: dict with keys:

        - ``hostname`` (str): the config section name passed in.
        - ``host`` (str): resolved host/IP from config.
        - ``ok`` (bool): True if the connection opened.
        - ``dev`` (:class:`jnpr.junos.Device` | None): live PyEZ Device when
          ``ok`` is True, else None. NOT JSON-serializable; programmatic
          consumers should strip before serializing.
        - ``error`` (str | None): exception class name when ``ok`` is False,
          else None.
        - ``error_message`` (str | None): human-readable error message when
          ``ok`` is False, else None.

    Does not print.
    """
    logger.debug("connect: start")
    host = config.get(hostname, "host")
    kwargs = {
        "host": host,
        "port": int(config.get(hostname, "port")),
        "user": config.get(hostname, "id"),
        "passwd": config.get(hostname, "pw"),
        "ssh_private_key_file": os.path.expanduser(config.get(hostname, "sshkey")),
        "huge_tree": config.getboolean(hostname, "huge_tree", fallback=False),
        "gather_facts": gather_facts,
    }
    # Pass ssh_config only when the operator sets it; leaving it out preserves
    # PyEZ/paramiko's implicit ~/.ssh/config auto-pickup.
    ssh_config_path = config.get(hostname, "ssh_config", fallback=None)
    if ssh_config_path:
        kwargs["ssh_config"] = os.path.expanduser(ssh_config_path)
    dev = Device(**kwargs)
    _ERROR_PREFIX = {
        ConnectAuthError: "Authentication credentials fail to login",
        ConnectRefusedError: "NETCONF Connection refused",
        ConnectTimeoutError: "Connection timeout",
        ConnectUnknownHostError: "Unknown Host",
        ConnectError: "Cannot connect to device",
    }
    try:
        dev.open()
        logger.debug(f"connect: ok dev={dev}")
        return {
            "hostname": hostname,
            "host": host,
            "ok": True,
            "dev": dev,
            "error": None,
            "error_message": None,
        }
    except Exception as e:
        err_name = type(e).__name__
        prefix = None
        for exc_type, text in _ERROR_PREFIX.items():
            if isinstance(e, exc_type):
                prefix = text
                break
        if prefix is not None:
            msg = f"{prefix}: {e}"
        else:
            msg = str(e)
        logger.debug(f"connect: error={err_name} msg={msg}")
        return {
            "hostname": hostname,
            "host": host,
            "ok": False,
            "dev": None,
            "error": err_name,
            "error_message": msg,
        }


def _get_host_tags(section: str) -> set[str]:
    """Return the set of tags for a config section."""
    raw = config.get(section, "tags", fallback="")
    if not raw.strip():
        return set()
    return {t.strip().lower() for t in raw.split(",")}


def _filter_by_tag_groups(tag_groups: list[set[str]]) -> list[str]:
    """Return sections that match any of the tag groups.

    Each element of ``tag_groups`` is a set of required tags. A section
    matches if its tag set is a superset of at least one group (OR
    across groups, AND within a group). Section order is preserved so
    host ordering stays stable.
    """
    matched = []
    for section in config.sections():
        host_tags = _get_host_tags(section)
        for group in tag_groups:
            if group <= host_tags:
                matched.append(section)
                break
    return matched


def _parse_tag_groups(tags) -> list[set[str]]:
    """Normalize the ``--tags`` CLI value into a list of tag sets.

    ``--tags`` is ``action="append"``, so the argparse value is either
    ``None`` (flag not used), a single string (legacy non-append
    consumers), or a list of strings (one per ``--tags`` occurrence).
    Each string is further comma-split, with whitespace trimmed and
    folded to lower case. Empty groups are dropped.
    """
    if tags is None:
        return []
    raw_groups = tags if isinstance(tags, list) else [tags]
    groups: list[set[str]] = []
    for raw in raw_groups:
        group = {t.strip().lower() for t in raw.split(",") if t.strip()}
        if group:
            groups.append(group)
    return groups


def get_targets():
    """Return target host list from CLI args, tags, or config sections."""
    tags = getattr(args, "tags", None)
    has_hosts = len(args.specialhosts) > 0

    tag_groups = _parse_tag_groups(tags)

    # パターン1: --tags なし & hosts なし → 全セクション（現行動作）
    if not tag_groups and not has_hosts:
        targets = []
        for i in config.sections():
            tmp = config.get(i, "host")
            logger.debug(f"{i=} {tmp=}")
            if tmp is not None:
                targets.append(i)
            else:
                print(i, "is not found in", args.config)
                sys.exit(1)
        return targets

    # パターン2: --tags なし & hosts あり → 指定ホストのみ（現行動作）
    if not tag_groups and has_hosts:
        targets = []
        for i in args.specialhosts:
            if config.has_section(i):
                tmp = config.get(i, "host")
            else:
                print(i, "is not found in", args.config)
                sys.exit(1)
            logger.debug(f"{i=} {tmp=}")
            targets.append(i)
        return targets

    # Pattern 3: --tags only -> union of each group's AND match.
    # Within a --tags group (comma-separated) tags AND together; multiple
    # --tags occurrences OR together. Example: --tags a,b --tags c
    # matches hosts with (a AND b) OR c.
    if tag_groups and not has_hosts:
        targets = _filter_by_tag_groups(tag_groups)
        if not targets:
            print("no hosts matched tags:", tags)
            sys.exit(1)
        return targets

    # Pattern 4: --tags + hosts -> intersection (tag filter AND name list).
    # Pre-0.16.4 was union; intersection is more intuitive ("narrow the
    # tag selection further by name") and keeps --tags as a safety rail.
    tag_matched = set(_filter_by_tag_groups(tag_groups))
    targets = []
    for i in args.specialhosts:
        if not config.has_section(i):
            print(i, "is not found in", args.config)
            sys.exit(1)
        if i in tag_matched:
            targets.append(i)
    if not targets:
        print("no hosts matched both tags and names:", tags, args.specialhosts)
        sys.exit(1)
    return targets


def load_commands(filepath: str) -> list[str]:
    """Load command lines from a file, stripping blank lines and comments.

    Lines starting with '#' are treated as comments and excluded.
    """
    with open(filepath) as f:
        return [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def render_template(filepath: str, hostname: str, dev) -> list[str]:
    """Render a Jinja2 template file with host variables and device facts.

    Variables come from:
    1. config.ini host section: keys starting with 'var_' (prefix stripped)
    2. Device facts: injected as 'facts' dict
    3. Built-in: 'hostname' (config section name)

    :param filepath: path to .j2 template file
    :param hostname: config section name
    :param dev: connected Device object (for dev.facts)
    :return: list of rendered command lines (blank/comment lines excluded)
    :raises ImportError: if Jinja2 is not installed
    """
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError:
        raise ImportError(
            "Jinja2 is required for template support. "
            "Install it with: pip install junos-ops[template]"
        )

    # テンプレート変数の構築
    variables = {"hostname": hostname, "facts": dict(dev.facts)}

    # config.ini の var_ プレフィックス変数を収集
    for key in config.options(hostname):
        if key.startswith("var_"):
            variables[key[4:]] = config.get(hostname, key)

    # テンプレートレンダリング
    template_dir = os.path.dirname(os.path.abspath(filepath))
    template_name = os.path.basename(filepath)
    env = Environment(
        loader=FileSystemLoader(template_dir),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_name)
    rendered = template.render(variables)

    # レンダリング結果をコマンドリストに変換（空行・コメント除去）
    return [
        line.strip() for line in rendered.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def run_parallel(func, targets, max_workers=1):
    """Run a function against targets using ThreadPoolExecutor.

    When max_workers=1, runs serially for backward compatibility.
    """
    if max_workers <= 1:
        results = {}
        for target in targets:
            results[target] = func(target)
        return results

    with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_target = {
            executor.submit(func, target): target
            for target in targets
        }
        results = {}
        for future in futures.as_completed(future_to_target):
            target = future_to_target[future]
            try:
                results[target] = future.result()
            except Exception as e:
                logger.error(f"{target} generated an exception: {e}")
                results[target] = 1
        return results
