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


def read_config():
    """Read and parse the INI config file."""
    global config
    config = configparser.ConfigParser(allow_no_value=True)
    config.read(args.config)
    if len(config.sections()) == 0:
        print(args.config, "is empty")
        return True
    for section in config.sections():
        if config.has_option(section, "host"):
            host = config.get(section, "host")
        else:
            host = None
        if host is None:
            # host is [section] name
            config.set(section, "host", section)
        for key in config[section]:
            logger.debug(f"{section} > {key} : {config[section][key]}")
    return False


def connect(hostname):
    """Open NETCONF connection to a device."""
    logger.debug("connect: start")
    dev = Device(
        host=config.get(hostname, "host"),
        port=int(config.get(hostname, "port")),
        user=config.get(hostname, "id"),
        passwd=config.get(hostname, "pw"),
        ssh_private_key_file=os.path.expanduser(config.get(hostname, "sshkey")),
        huge_tree=config.getboolean(hostname, "huge_tree", fallback=False),
    )
    err = None
    try:
        dev.open()
        err = False
    except ConnectAuthError as e:
        print("Authentication credentials fail to login: {0}".format(e))
        dev = None
        err = True
    except ConnectRefusedError as e:
        print("NETCONF Connection refused: {0}".format(e))
        dev = None
        err = True
    except ConnectTimeoutError as e:
        print("Connection timeout: {0}".format(e))
        dev = None
        err = True
    except ConnectError as e:
        print("Cannot connect to device: {0}".format(e))
        dev = None
        err = True
    except ConnectUnknownHostError as e:
        print("Unknown Host: {0}".format(e))
        dev = None
        err = True
    except Exception as e:
        print(e)
        dev = None
        err = True
    logger.debug(f"connect: err={err} dev={dev}")
    logger.debug("connect: end")
    return err, dev


def _get_host_tags(section: str) -> set[str]:
    """Return the set of tags for a config section."""
    raw = config.get(section, "tags", fallback="")
    if not raw.strip():
        return set()
    return {t.strip().lower() for t in raw.split(",")}


def _filter_by_tags(required_tags: set[str]) -> list[str]:
    """Return sections whose tags are a superset of required_tags (AND)."""
    matched = []
    for section in config.sections():
        if required_tags <= _get_host_tags(section):
            matched.append(section)
    return matched


def get_targets():
    """Return target host list from CLI args, tags, or config sections."""
    tags = getattr(args, "tags", None)
    has_hosts = len(args.specialhosts) > 0

    # タグ指定時: パースして AND フィルタ用の set を作成
    if tags is not None:
        required_tags = {t.strip().lower() for t in tags.split(",")}
    else:
        required_tags = set()

    # パターン1: --tags なし & hosts なし → 全セクション（現行動作）
    if not required_tags and not has_hosts:
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
    if not required_tags and has_hosts:
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

    # パターン3: --tags あり & hosts なし → タグで AND フィルタ
    if required_tags and not has_hosts:
        targets = _filter_by_tags(required_tags)
        if not targets:
            print("no hosts matched tags:", tags)
            sys.exit(1)
        return targets

    # パターン4: --tags あり & hosts あり → タグフィルタ結果 ∪ hosts（重複排除）
    tag_matched = _filter_by_tags(required_tags)
    seen = set()
    targets = []
    # タグマッチ分を先に追加
    for i in tag_matched:
        if i not in seen:
            seen.add(i)
            targets.append(i)
    # 明示指定ホストを追加（存在チェック付き）
    for i in args.specialhosts:
        if not config.has_section(i):
            print(i, "is not found in", args.config)
            sys.exit(1)
        if i not in seen:
            seen.add(i)
            targets.append(i)
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
