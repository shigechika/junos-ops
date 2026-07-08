#
#   Copyright ©︎2022-2025 AIKAWA Shigechika
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""CLI entry point and subcommand routing for junos-ops."""

from jnpr.junos.exception import ConnectClosedError
import argparse
import io
import sys
import logging
import logging.config
import os

def _find_logging_ini():
    """Search for logging.ini in standard locations."""
    if os.path.isfile("logging.ini"):
        return "logging.ini"
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    xdg_path = os.path.join(xdg, "junos-ops", "logging.ini")
    if os.path.isfile(xdg_path):
        return xdg_path
    return None

_logging_ini = _find_logging_ini()
if _logging_ini:
    logging.config.fileConfig(_logging_ini)
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
# ncclient / paramiko / junos-eznc emit every NETCONF and SSH frame at
# INFO.  Apply WARNING suppression regardless of whether logging.ini was
# found: when logging.ini sets root=DEBUG but omits these loggers, they
# inherit DEBUG and flood the terminal.  Only override loggers that have
# no explicit level (NOTSET) so a deliberate logging.ini entry is honoured.
for noisy in ("ncclient", "paramiko", "jnpr.junos"):
    lgr = logging.getLogger(noisy)
    if lgr.level == logging.NOTSET:
        lgr.setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from junos_ops import __version__ as version  # noqa: E402
from junos_ops import common  # noqa: E402
from junos_ops import display  # noqa: E402
from junos_ops import upgrade  # noqa: E402
from junos_ops import snapshot  # noqa: E402
from junos_ops import rsi  # noqa: E402
from junos_ops import show  # noqa: E402


# --- サブコマンド用エントリ関数 ---


def _json_mode() -> bool:
    """Return True when ``--json`` machine-readable output is requested."""
    return getattr(common.args, "json", False)


def _route_logs_to_stderr() -> None:
    """Redirect any stdout-bound logging StreamHandler to stderr.

    Under ``--json`` stdout must carry only JSON lines, but both the
    shipped logging.ini console handler and the basicConfig fallback
    write log records to stdout — and ``load_config`` streams progress
    via ``logger.info``. Moving those handlers to stderr keeps logs as
    diagnostics while stdout stays machine-parseable. A file handler's
    stream is not ``sys.stdout`` so it is left untouched.
    """
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout:
            h.setStream(sys.stderr)


def _emit_result(hostname: str, result: dict, formatter) -> None:
    """Emit a core result as a JSON line (``--json``) or formatted text.

    ``formatter`` is the matching ``display.format_*`` callable used for
    the human-readable path.
    """
    if _json_mode():
        display.print_json(hostname, result)
    else:
        display.print_host_block(hostname, formatter(result))


def _emit_exception(hostname: str, exc: Exception) -> None:
    """Report a worker-level exception as JSON (``--json``) or a log record.

    In JSON mode a failed host emits an explicit error object so a JSONL
    consumer sees a record rather than a silently missing line.
    """
    if _json_mode():
        display.print_json(
            hostname,
            {"ok": False, "error": type(exc).__name__, "error_message": str(exc)},
        )
    else:
        logger.error(f"{hostname}: {exc}")


def _open_connection(hostname: str):
    """Open a NETCONF connection and render any error via the display layer.

    :return: live :class:`jnpr.junos.Device` on success, None on failure
        (after the error has already been printed through the display layer).
    """
    conn = common.connect(hostname)
    if not conn["ok"]:
        if _json_mode():
            display.print_json(
                hostname,
                {
                    "ok": False,
                    "phase": "connect",
                    "error": conn.get("error"),
                    "error_message": conn.get("error_message"),
                },
            )
        else:
            display.print_host_block(hostname, display.format_connect_error(conn))
        return None
    return conn["dev"]


def cmd_facts(hostname) -> int:
    """Display device facts."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        if _json_mode():
            display.print_json(hostname, dict(dev.facts))
        else:
            # print_facts emits the header + facts under _print_lock as one
            # atomic block, so parallel workers (--workers N) cannot interleave
            # another host's output between this host's header and body.
            display.print_facts(hostname, dev.facts)
        return 0
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_upgrade(hostname) -> int:
    """Copy and install package."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.install(hostname, dev)
        _emit_result(hostname, result, display.format_install)
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_copy(hostname) -> int:
    """Copy package to remote device."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.copy(hostname, dev)
        _emit_result(hostname, result, display.format_copy)
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_install(hostname) -> int:
    """Install previously copied package."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.install(hostname, dev)
        _emit_result(hostname, result, display.format_install)
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_rollback(hostname) -> int:
    """Rollback to previous version."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        pending = upgrade.get_pending_version(hostname, dev)
        if pending is None:
            if _json_mode():
                display.print_json(
                    hostname, {"ok": True, "pending": None, "skipped": True}
                )
            else:
                display.print_host_block(
                    hostname,
                    "rollback: pending version is None\nrollback: skip",
                )
            return 0
        result = upgrade.rollback(hostname, dev)
        if _json_mode():
            display.print_json(hostname, {"pending": pending, **result})
            return 0 if result.get("ok") else 1
        lines = [f"rollback: pending version is {pending}"]
        rb = display.format_rollback(result)
        if rb:
            lines.append(rb)
        if result.get("ok") and not common.args.dry_run:
            lines.append("rollback: successful")
        display.print_host_block(hostname, "\n".join(lines))
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_version(hostname) -> int:
    """Show device version information."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.show_version(hostname, dev)
        _emit_result(hostname, result, display.format_version)
        return 0
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_reboot(hostname) -> int:
    """Schedule device reboot."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.reboot(hostname, dev, common.args.rebootat)
        _emit_result(hostname, result, display.format_reboot)
        return result.get("code", 1)
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_snapshot(hostname) -> int:
    """Create a recovery snapshot (request system snapshot)."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = snapshot.create_snapshot(hostname, dev)
        _emit_result(hostname, result, display.format_snapshot)
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_show(hostname) -> int:
    """Run CLI command on device and print output via display layer."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    retry = getattr(common.args, "retry", 0)
    output_format = getattr(common.args, "show_format", "text") or "text"
    try:
        if common.args.showfile:
            commands = common.load_commands(common.args.showfile)
            result = show.run_cli_batch(
                dev,
                commands,
                output_format=output_format,
                retry=retry,
                hostname=hostname,
            )
        else:
            result = show.run_cli(
                dev,
                common.args.show_command,
                output_format=output_format,
                retry=retry,
                hostname=hostname,
            )
        if _json_mode():
            display.print_json(hostname, result)
        else:
            display.print_show(result)
        return 0 if result["ok"] else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_config(hostname) -> int:
    """Push set command file to device."""
    if getattr(common.args, "no_commit", False) and getattr(common.args, "no_confirm", False):
        msg = "--no-commit and --no-confirm are mutually exclusive"
        if _json_mode():
            display.print_json(
                hostname, {"ok": False, "error": "MutuallyExclusiveArgs", "error_message": msg}
            )
        else:
            display.print_host_block(hostname, f"\t{msg}")
        return 1
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        # RPC タイムアウトの設定（CLI > config.ini > デフォルト 120秒）
        timeout = getattr(common.args, "rpc_timeout", None)
        if timeout is None:
            try:
                timeout = int(common.config.get(hostname, "timeout"))
            except Exception:
                pass
        if timeout is None:
            timeout = 120
        dev.timeout = timeout
        result = upgrade.load_config(hostname, dev, common.args.configfile)
        _emit_result(hostname, result, display.format_load_config)
        return 0 if result.get("ok") else 1
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def _run_check_with_progress(targets, max_workers: int) -> dict:
    """Run ``_check_host`` over targets with a tqdm progress bar.

    Falls back to plain :func:`common.run_parallel` when tqdm is not
    installed or stderr is not a TTY (CI / piped output). Each
    completion writes a one-line summary above the bar via
    ``tqdm.write`` so users see parallel progress, not just a counter.
    """
    use_tqdm = sys.stderr.isatty()
    if use_tqdm:
        try:
            from tqdm import tqdm
        except ImportError:
            use_tqdm = False

    if not use_tqdm:
        return common.run_parallel(
            _check_host, targets, max_workers=max_workers
        )

    from concurrent import futures as _futures
    results: dict = {}
    with _futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_host = {ex.submit(_check_host, t): t for t in targets}
        with tqdm(
            total=len(targets),
            desc="check",
            unit="host",
            file=sys.stderr,
            dynamic_ncols=True,
        ) as bar:
            for fut in _futures.as_completed(future_to_host):
                host = future_to_host[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    logger.error(f"{host}: {e}")
                    row = {
                        "hostname": host,
                        "model": None,
                        "model_source": None,
                        "connect": {
                            "ok": False,
                            "message": str(e),
                            "error": type(e).__name__,
                        },
                        "remote": None,
                    }
                results[host] = row
                conn = row.get("connect") or {}
                conn_status = "ok" if conn.get("ok") else "fail"
                model = row.get("model") or "-"
                bar.set_postfix_str(f"{host} {conn_status} {model}")
                bar.update(1)
    return results


def _fetch_model_cheap(dev) -> str | None:
    """Fetch only the device model via a single ``get-software-information``
    RPC (~100 ms) instead of paying the full PyEZ facts cost (~10 RPCs).

    Returns the ``product-model`` text, or None if the RPC fails or
    the field is absent.
    """
    try:
        rpc = dev.rpc.get_software_information()
        elem = rpc.find(".//product-model")
        return elem.text if elem is not None else None
    except Exception as e:
        logger.debug(f"get-software-information failed: {e}")
        return None


def _check_host(hostname) -> dict:
    """Worker for the ``check`` subcommand (per-host checks).

    Handles ``--connect`` and ``--remote`` against a single host.
    ``--local`` is inventory-based and is processed separately by
    :func:`_check_local_inventory`; this worker ignores it.
    """
    do_connect = common.args.check_connect
    do_remote = common.args.check_remote
    explicit_model = getattr(common.args, "check_model", None)

    result = {
        "hostname": hostname,
        "model": None,
        "model_source": None,
        "connect": None,
        "remote": None,
        "disk": None,
    }

    # Model resolution order: --model > config.ini [host].model > device facts.
    model = explicit_model
    source = "cli" if model else None
    if model is None:
        try:
            cfg_model = common.config.get(hostname, "model")
            if cfg_model:
                model = cfg_model
                source = "config"
        except Exception:
            pass

    dev = None
    if do_connect or do_remote:
        # PyEZ の Device.open() は gather_facts=True がデフォルトで、開いた
        # 瞬間に chassis inventory 等 ~10 RPC を流す。model がもう確定して
        # いる、または --remote が指定されていない場合は facts は不要なので
        # gather_facts=False で開いて handshake のみにする。
        need_facts = do_remote and model is None
        # auto_probe=5: 不通ホストを TCP-level の 5 秒で fail させ、OS
        # デフォルトの ~60-120 秒 SYN タイムアウトで全体が引きずられないように。
        conn = common.connect(
            hostname, gather_facts=need_facts, auto_probe=5
        )
        if conn["ok"]:
            dev = conn["dev"]
            result["connect"] = {
                "ok": True,
                "message": "connected",
                "error": None,
            }
            if need_facts:
                try:
                    model = dev.facts.get("model")
                    if model:
                        source = "device"
                except Exception as e:
                    logger.debug(f"{hostname}: facts access failed: {e}")
            elif model is None:
                # gather_facts=False のままで model だけ欲しい: 単発 RPC で取得。
                cheap = _fetch_model_cheap(dev)
                if cheap:
                    model = cheap
                    source = "device"
        else:
            result["connect"] = {
                "ok": False,
                "message": conn.get("error_message") or "connect failed",
                "error": conn.get("error"),
            }

    result["model"] = model
    result["model_source"] = source

    try:
        if do_remote:
            if dev is not None and model:
                try:
                    result["remote"] = upgrade.check_remote_package_by_model(
                        hostname, dev, model
                    )
                except Exception as e:
                    # config に <model>.file / .hash が無い等の lookup 失敗を
                    # 接続失敗扱いにすると誤解を招くので、unchecked で記録。
                    result["remote"] = {
                        "status": "unchecked",
                        "message": f"recipe lookup failed: {e}",
                        "file": None,
                        "cached": False,
                        "error": type(e).__name__,
                    }
            else:
                reason = "not connected" if dev is None else "model unknown"
                result["remote"] = {
                    "status": "unchecked",
                    "message": reason,
                    "file": None,
                    "cached": False,
                }

        if dev is not None:
            result["disk"] = upgrade.get_disk_avail(hostname, dev)
    finally:
        if dev is not None:
            try:
                dev.close()
            except (ConnectClosedError, Exception):
                pass

    return result


def _check_local_inventory() -> list[dict]:
    """Inventory-mode ``check --local``: iterate model→file map in ``config.ini``.

    Hostnames are ignored — the local firmware map is an attribute of
    the staging server, not the devices. Verifies each model's
    ``<model>.file`` against its ``<model>.hash`` using ``"DEFAULT"``
    as the config section so DEFAULT-level ``lpath`` / ``hashalgo``
    apply.

    Model selectors compose as: ``--model`` (single name), ``--tags``
    / ``--exclude-tags`` (group-AND / OR over ``<model>.tags`` + the
    model name itself), and all of them intersect. ``--tags
    ex2300-24t`` matches by model name even when ``ex2300-24t.tags``
    is unset; ``--tags main`` requires ``<model>.tags`` to mention
    ``main``. When the resulting set is empty we log it via
    ``logger.info`` so the operator knows *why* zero rows came back
    instead of guessing.
    """
    filter_model = getattr(common.args, "check_model", None)
    tags = getattr(common.args, "tags", None)
    exclude_tags = getattr(common.args, "exclude_tags", None)

    if filter_model:
        # iter_configured_models() yields lowercase names (configparser
        # lowercases keys), and the firmware lookup is case-insensitive
        # too. Normalize the --model value so the rendered model column
        # matches the default listing instead of echoing the user's
        # casing (e.g. EX2300-24T vs ex2300-24t for the same model).
        models = [filter_model.lower()]
    else:
        models = upgrade.iter_configured_models()

    tag_groups = common._parse_tag_groups(tags)
    exclude_groups = common._parse_tag_groups(exclude_tags)

    if tag_groups:
        models = common._filter_models_by_tag_groups(models, tag_groups)
    if exclude_groups:
        dropped = set(common._filter_models_by_tag_groups(models, exclude_groups))
        models = [m for m in models if m not in dropped]

    if (tag_groups or exclude_groups or filter_model) and not models:
        logger.info(
            "check --local: no models matched after filtering "
            "(--model=%s --tags=%s --exclude-tags=%s)",
            filter_model, tags, exclude_tags,
        )

    rows: list[dict] = []
    for model in models:
        try:
            result = upgrade.check_local_package_by_model("DEFAULT", model)
        except Exception as e:
            rows.append({
                "model": model,
                "file": None,
                "local_file": None,
                "status": "error",
                "cached": False,
                "actual_hash": None,
                "expected_hash": None,
                "message": f"config lookup failed: {e}",
                "error": type(e).__name__,
            })
            continue
        rows.append({
            "model": model,
            "file": result.get("file"),
            "local_file": result.get("local_file"),
            "status": result.get("status"),
            "cached": result.get("cached"),
            "actual_hash": result.get("actual_hash"),
            "expected_hash": result.get("expected_hash"),
            "message": result.get("message"),
            "error": result.get("error"),
        })
    return rows


def cmd_ls(hostname) -> int:
    """List remote files."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        result = upgrade.list_remote_path(hostname, dev)
        _emit_result(hostname, result, display.format_list_remote)
        return 0
    except Exception as e:
        _emit_exception(hostname, e)
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


# --- メイン ---


def main():
    """CLI entry point. Wraps :func:`_run` with graceful Ctrl-C handling."""
    try:
        return _run()
    except KeyboardInterrupt:
        sys.stderr.write("\naborted\n")
        return 130


def _run():
    """CLI dispatcher body."""
    # 共通オプション用の親パーサー
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-c", "--config", default=None, type=str,
        help="config filename (default: config.ini or ~/.config/junos-ops/config.ini)",
    )
    parent.add_argument(
        "-n", "--dry-run", action="store_true",
        help="connect and message output. No execute.",
    )
    parent.add_argument("-d", "--debug", action="store_true", help="debug output")
    parent.add_argument(
        "--json", action="store_true",
        help=(
            "emit machine-readable JSON instead of human-readable text "
            "(one JSON object per host per line; pipe to `jq -s` to slurp). "
            "Logs are redirected to stderr so stdout stays pure JSON."
        ),
    )
    parent.add_argument(
        "--force", action="store_true", help="force execute",
    )
    parent.add_argument(
        "--workers", type=int, default=None,
        help="parallel workers (default: 1 for upgrade, 20 for rsi/check)",
    )
    parent.add_argument(
        "--tags", type=str, default=None, action="append",
        help=(
            "filter hosts by tags. Within a single --tags value, "
            "comma-separated tags AND together. Repeat --tags to OR "
            "groups: --tags a,b --tags c = (a AND b) OR c."
        ),
    )
    parent.add_argument(
        "--exclude-tags", type=str, default=None, action="append",
        dest="exclude_tags",
        help=(
            "exclude hosts whose tags match. Same AND/OR grammar as --tags "
            "(comma = AND within a group, repeat to OR groups). Applied "
            "after --tags. Usable on its own to drop a subset from the "
            "default 'all hosts' selection: --exclude-tags srx345."
        ),
    )

    parser = argparse.ArgumentParser(
        description="junos-ops: Juniper Networks デバイス管理ツール",
        epilog="サブコマンド省略時はデバイス情報を表示します",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + version)
    subparsers = parser.add_subparsers(dest="subcommand")

    # upgrade
    p_upgrade = subparsers.add_parser(
        "upgrade", parents=[parent], help="copy and install package",
    )
    p_upgrade.add_argument("specialhosts", metavar="hostname", nargs="*")
    p_upgrade.add_argument(
        "--unlink",
        action="store_true",
        help=(
            "explicitly pass 'unlink' to 'request system software add'. "
            "Required for some low-flash devices (EX2300/EX3400) where "
            "PyEZ default unlink behavior is incomplete."
        ),
    )

    # copy
    p_copy = subparsers.add_parser(
        "copy", parents=[parent], help="copy package to remote",
    )
    p_copy.add_argument("specialhosts", metavar="hostname", nargs="*")

    # install
    p_install = subparsers.add_parser(
        "install", parents=[parent], help="install copied package",
    )
    p_install.add_argument("specialhosts", metavar="hostname", nargs="*")
    p_install.add_argument(
        "--unlink",
        action="store_true",
        help=(
            "explicitly pass 'unlink' to 'request system software add'. "
            "Required for some low-flash devices (EX2300/EX3400) where "
            "PyEZ default unlink behavior is incomplete."
        ),
    )

    # rollback
    p_rollback = subparsers.add_parser(
        "rollback", parents=[parent], help="rollback installed package",
    )
    p_rollback.add_argument("specialhosts", metavar="hostname", nargs="*")

    # version
    p_version = subparsers.add_parser(
        "version", parents=[parent], help="show device version",
    )
    p_version.add_argument("specialhosts", metavar="hostname", nargs="*")

    # reboot
    p_reboot = subparsers.add_parser(
        "reboot", parents=[parent], help="reboot device",
    )
    p_reboot.add_argument(
        "--at", dest="rebootat", required=True,
        type=upgrade.yymmddhhmm_type,
        help="reboot at yymmddhhmm (e.g. 2501020304)",
    )
    p_reboot.add_argument("specialhosts", metavar="hostname", nargs="*")

    # snapshot
    p_snapshot = subparsers.add_parser(
        "snapshot", parents=[parent],
        help=(
            "create a recovery snapshot (request system snapshot) to sync the "
            "alternate boot media; MX-focused. Refuses if running on the "
            "alternate media unless --force."
        ),
    )
    p_snapshot.add_argument("specialhosts", metavar="hostname", nargs="*")

    # ls
    p_ls = subparsers.add_parser(
        "ls", parents=[parent], help="list remote files",
    )
    p_ls.add_argument(
        "-l", action="store_const", dest="list_format", const="long", default="short",
        help="long format (like ls -l)",
    )
    p_ls.add_argument("specialhosts", metavar="hostname", nargs="*")

    # show
    p_show = subparsers.add_parser(
        "show", parents=[parent], help="run CLI command on devices",
    )
    p_show.add_argument(
        "-f", "--file", dest="showfile", metavar="FILE",
        help="path to file containing CLI commands (one per line)",
    )
    p_show.add_argument(
        "-F", "--format", dest="show_format",
        choices=list(show.VALID_FORMATS), default="text",
        help=(
            "output format: text (default), json, or xml. "
            "Note: '| match' / '| last' pipe stages are dropped by NETCONF "
            "regardless of format; filter client-side or call an RPC "
            "directly when you need to filter."
        ),
    )
    p_show.add_argument(
        "--retry", type=int, default=0,
        help="number of retries on RpcTimeoutError (default: 0)",
    )
    p_show.add_argument(
        "show_args", metavar="command_or_hostname", nargs="*",
        help='CLI command (quoted) followed by hostnames, or hostnames only with -f',
    )

    # config
    p_config = subparsers.add_parser(
        "config", parents=[parent], help="push set command file to devices",
    )
    p_config.add_argument(
        "-f", "--file", dest="configfile", required=True, metavar="FILE",
        help="path to set command file",
    )
    p_config.add_argument(
        "--confirm", dest="confirm_timeout", type=int, default=1,
        help="commit confirm timeout in minutes (default: 1)",
    )
    p_config.add_argument(
        "--health-check", dest="health_check",
        action="append", default=None,
        help='health check after commit confirmed '
             '(repeatable, tries in order; '
             '"uptime" for NETCONF uptime probe, '
             'or any CLI command; '
             'default: "uptime")',
    )
    p_config.add_argument(
        "--no-health-check", dest="no_health_check",
        action="store_true",
        help="skip health check after commit confirmed",
    )
    p_config.add_argument(
        "--timeout", dest="rpc_timeout", type=int, default=None,
        help="RPC timeout in seconds (default: 120, or 'timeout' in config.ini)",
    )
    p_config.add_argument(
        "--no-confirm", dest="no_confirm", action="store_true",
        help="skip commit confirmed and health check, commit directly",
    )
    p_config.add_argument(
        "--no-commit", dest="no_commit", action="store_true",
        help="apply with commit confirmed but skip final commit "
             "(JUNOS auto-rolls back after --confirm minutes)",
    )
    p_config.add_argument("specialhosts", metavar="hostname", nargs="*")

    # check
    p_check = subparsers.add_parser(
        "check",
        parents=[parent],
        help="pre-flight checks (NETCONF reachability, local/remote firmware checksum)",
    )
    p_check.add_argument(
        "--connect", dest="check_connect", action="store_true",
        help="verify NETCONF reachability (default when no flag is given)",
    )
    p_check.add_argument(
        "--local", dest="check_local", action="store_true",
        help="verify local firmware checksum (no NETCONF required)",
    )
    p_check.add_argument(
        "--remote", dest="check_remote", action="store_true",
        help="verify remote firmware checksum on the device (requires NETCONF)",
    )
    p_check.add_argument(
        "--all", dest="check_all", action="store_true",
        help="shorthand for --connect --local --remote",
    )
    p_check.add_argument(
        "--model", dest="check_model", default=None,
        help="override model lookup (otherwise from config.ini 'model' or device facts)",
    )
    p_check.add_argument("specialhosts", metavar="hostname", nargs="*")

    # rsi
    p_rsi = subparsers.add_parser(
        "rsi", parents=[parent], help="collect RSI/SCF",
    )
    p_rsi.add_argument(
        "--rsi-dir", dest="rsi_dir", default=None,
        help="output directory for RSI/SCF files",
    )
    p_rsi.add_argument("specialhosts", metavar="hostname", nargs="*")

    # サブコマンドなし → device facts 表示
    # argparse はサブコマンドなしで positional args を受け取れないため、
    # 引数がサブコマンドに一致しない場合は facts として扱う
    # Tab completion (requires: pip install junos-ops[completion])
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    # argparse はサブコマンドに一致しない位置引数があると SystemExit を
    # 送出する（例: junos-ops -c accounts.ini → accounts.ini がサブコマンド
    # として解釈される）。この場合はサブコマンドなしとして再パースする。
    _saved_stderr = sys.stderr
    try:
        sys.stderr = io.StringIO()
        args, unknown = parser.parse_known_args()
    except SystemExit as e:
        if e.code == 0:
            sys.stderr = _saved_stderr
            raise
        args = argparse.Namespace(subcommand=None)
        unknown = []
    finally:
        sys.stderr = _saved_stderr

    # show サブコマンド: 余剰位置引数を show_args に統合
    # （argparse は nargs="*" でもオプション後の位置引数を正しく収集できないため）
    if unknown:
        if getattr(args, "subcommand", None) == "show":
            args.show_args = getattr(args, "show_args", []) + unknown
        elif getattr(args, "subcommand", None) is not None:
            parser.error(f"unrecognized arguments: {' '.join(unknown)}")
        # subcommand is None: 後続の facts_parser 再パースで処理

    # サブコマンドなしの場合の処理
    if args.subcommand is None:
        # サブコマンドなしで hostname が指定されたケースを処理
        # 例: junos-ops hostname1 hostname2
        remaining = sys.argv[1:]
        if remaining and not remaining[0].startswith("-"):
            # 親パーサーで再パース
            facts_parser = argparse.ArgumentParser(parents=[parent], add_help=False)
            facts_parser.add_argument("specialhosts", metavar="hostname", nargs="*")
            args = facts_parser.parse_args()
            args.subcommand = None
        else:
            # オプションのみ or 引数なし
            if not remaining:
                parser.print_help()
                return 0
            # -c や -d 等のオプションのみ → facts として解釈
            facts_parser = argparse.ArgumentParser(parents=[parent], add_help=False)
            facts_parser.add_argument("specialhosts", metavar="hostname", nargs="*")
            try:
                args = facts_parser.parse_args()
            except SystemExit:
                parser.print_help()
                return 0
            args.subcommand = None

    # 後方互換属性の設定
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "list_format"):
        args.list_format = None
    if not hasattr(args, "rebootat"):
        args.rebootat = None
    if not hasattr(args, "rsi_dir"):
        args.rsi_dir = None
    if not hasattr(args, "configfile"):
        args.configfile = None
    if not hasattr(args, "confirm_timeout"):
        args.confirm_timeout = 1
    if not hasattr(args, "health_check"):
        args.health_check = None
    if not hasattr(args, "no_health_check"):
        args.no_health_check = False
    if not hasattr(args, "show_command"):
        args.show_command = None
    if not hasattr(args, "showfile"):
        args.showfile = None
    if not hasattr(args, "show_format"):
        args.show_format = "text"
    if not hasattr(args, "tags"):
        args.tags = None
    if not hasattr(args, "exclude_tags"):
        args.exclude_tags = None
    if not hasattr(args, "retry"):
        args.retry = 0
    if not hasattr(args, "rpc_timeout"):
        args.rpc_timeout = None
    if not hasattr(args, "no_confirm"):
        args.no_confirm = False
    if not hasattr(args, "no_commit"):
        args.no_commit = False
    if not hasattr(args, "check_connect"):
        args.check_connect = False
    if not hasattr(args, "check_local"):
        args.check_local = False
    if not hasattr(args, "check_remote"):
        args.check_remote = False
    if not hasattr(args, "check_all"):
        args.check_all = False
    if not hasattr(args, "check_model"):
        args.check_model = None

    # check サブコマンドのフラグ解決
    if args.subcommand == "check":
        if args.check_all:
            args.check_connect = True
            args.check_local = True
            args.check_remote = True
        # フラグ未指定なら --connect をデフォルト
        if not (args.check_connect or args.check_local or args.check_remote):
            args.check_connect = True

    # show サブコマンド: show_args を show_command + specialhosts に分離
    if args.subcommand == "show":
        show_args = getattr(args, "show_args", [])
        if args.showfile:
            # -f 使用時は位置引数をすべてホスト名として扱う
            args.show_command = None
            args.specialhosts = show_args
        elif show_args:
            # 最初の位置引数がコマンド、残りがホスト名
            args.show_command = show_args[0]
            args.specialhosts = show_args[1:]
        else:
            parser.error("show: コマンドまたは -f のいずれかを指定してください")

    common.args = args
    if common.args.config is None:
        common.args.config = common.get_default_config()

    # --json: stdout must carry only JSON, so move log records to stderr.
    if _json_mode():
        _route_logs_to_stderr()

    logger.debug("start")

    cfg_result = common.read_config()
    if not cfg_result["ok"]:
        if _json_mode():
            # Startup error → diagnostic on stderr so stdout stays pure JSON.
            print(display.format_read_config_error(cfg_result), file=sys.stderr)
        else:
            display.print_read_config_error(cfg_result)
        sys.exit(1)

    # check --local is host-independent (staging-server inventory). When
    # --connect / --remote are not also requested, skip the host
    # selector so that --tags / --exclude-tags can carry the *model*
    # filter for --local without get_targets() bailing on "no hosts
    # matched tags".
    local_only_check = (
        args.subcommand == "check"
        and getattr(common.args, "check_local", False)
        and not getattr(common.args, "check_connect", False)
        and not getattr(common.args, "check_remote", False)
    )
    if local_only_check:
        targets = []
    else:
        targets = common.get_targets()

    # workers のデフォルト値設定
    if common.args.workers is None:
        if args.subcommand in ("rsi", "check"):
            # I/O バウンドかつ副作用なしなので並列化しても安全
            common.args.workers = 20
        else:
            common.args.workers = 1

    # check サブコマンドは専用処理（local はインベントリ、connect/remote は per-host）
    if args.subcommand == "check":
        rc = 0

        json_mode = _json_mode()

        # --local: staging server 側のファームウェア棚卸し（ホスト非依存）
        if common.args.check_local:
            inventory = _check_local_inventory()
            if json_mode:
                # Inventory rows are model-keyed, not host-keyed; emit each
                # as-is (it already carries a "model" field).
                for row in inventory:
                    display.print_json_obj({"check": "local", **row})
            else:
                display.print_check_local_inventory(inventory)
            for row in inventory:
                if row.get("status") in ("bad", "missing", "error"):
                    rc = 1

        # --connect / --remote: ホスト単位でチェック
        if common.args.check_connect or common.args.check_remote:
            if common.args.check_local and not json_mode:
                # 2 つ目のテーブルの前にブランク行
                print("")
            results = _run_check_with_progress(
                targets, max_workers=common.args.workers
            )
            rows = [results[t] for t in targets if t in results]
            if json_mode:
                # Per-host rows already carry "hostname"; emit each as a line.
                for row in rows:
                    display.print_json_obj({"check": "host", **row})
            else:
                display.print_check_table(
                    rows,
                    show_connect=common.args.check_connect,
                    show_local=False,
                    show_remote=common.args.check_remote,
                    show_disk=common.args.check_connect or common.args.check_remote,
                )
            for row in rows:
                if not isinstance(row, dict):
                    rc = 1
                    continue
                conn = row.get("connect")
                if conn is not None and not conn.get("ok"):
                    rc = 1
                sub = row.get("remote")
                if sub and sub.get("status") in ("bad", "missing", "error"):
                    rc = 1

        logger.debug("end")
        return rc

    # サブコマンドのディスパッチ
    dispatch = {
        "upgrade": cmd_upgrade,
        "copy": cmd_copy,
        "install": cmd_install,
        "rollback": cmd_rollback,
        "version": cmd_version,
        "reboot": cmd_reboot,
        "snapshot": cmd_snapshot,
        "ls": cmd_ls,
        "show": cmd_show,
        "config": cmd_config,
        "rsi": rsi.cmd_rsi,
        None: cmd_facts,
    }

    func = dispatch.get(args.subcommand, cmd_facts)
    results = common.run_parallel(func, targets, max_workers=common.args.workers)

    # いずれかのホストが非0を返したら非0で終了
    for host, ret in results.items():
        if ret != 0:
            logger.debug(f"{host} returned {ret}")
            sys.exit(ret)

    logger.debug("end")
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
