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

from jnpr.junos.exception import ConnectClosedError, RpcTimeoutError
from pprint import pprint
import argparse
import io
import sys
import time
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
logger = logging.getLogger(__name__)

from junos_ops import __version__ as version  # noqa: E402
from junos_ops import common  # noqa: E402
from junos_ops import display  # noqa: E402
from junos_ops import upgrade  # noqa: E402
from junos_ops import rsi  # noqa: E402


# --- サブコマンド用エントリ関数 ---


def _open_connection(hostname: str):
    """Open a NETCONF connection and render any error via the display layer.

    :return: live :class:`jnpr.junos.Device` on success, None on failure
        (after the error has already been printed through the display layer).
    """
    conn = common.connect(hostname)
    if not conn["ok"]:
        display.print_host_header(hostname)
        display.print_connect_error(conn)
        return None
    return conn["dev"]


def cmd_facts(hostname) -> int:
    """Display device facts."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        print(f"# {hostname}")
        pprint(dev.facts)
        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        result = upgrade.install(hostname, dev)
        display.print_install(result)
        return 0 if result.get("ok") else 1
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        result = upgrade.copy(hostname, dev)
        display.print_copy(result)
        return 0 if result.get("ok") else 1
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        result = upgrade.install(hostname, dev)
        display.print_install(result)
        return 0 if result.get("ok") else 1
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        pending = upgrade.get_pending_version(hostname, dev)
        print(f"rollback: pending version is {pending}")
        if pending is None:
            print("rollback: skip")
            return 0
        result = upgrade.rollback(hostname, dev)
        display.print_rollback(result)
        if not result.get("ok"):
            return 1
        if not common.args.dry_run:
            print("rollback: successful")
        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        result = upgrade.show_version(hostname, dev)
        display.print_version(result)
        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
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
        display.print_host_header(hostname)
        result = upgrade.reboot(hostname, dev, common.args.rebootat)
        display.print_reboot(result)
        return result.get("code", 1)
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def _cli_with_retry(dev, command, hostname, max_retries):
    """Execute CLI command with retry on RpcTimeoutError.

    :param max_retries: Number of retries (0 = no retry).
    :raises: RpcTimeoutError if all retries exhausted.
    """
    for attempt in range(max_retries + 1):
        try:
            return dev.cli(command)
        except RpcTimeoutError:
            if attempt < max_retries:
                wait = 5 * (attempt + 1)
                logger.warning(
                    f"{hostname}: RpcTimeoutError, "
                    f"retry {attempt + 1}/{max_retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                raise


def cmd_show(hostname) -> int:
    """Run CLI command on device and print output."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    retry = getattr(common.args, "retry", 0)
    try:
        if common.args.showfile:
            # ファイルから複数コマンドを読み込み、1セッション内で順次実行
            commands = common.load_commands(common.args.showfile)
            lines = []
            for cmd in commands:
                output = _cli_with_retry(dev, cmd, hostname, retry)
                lines.append(f"## {cmd}\n{output.strip()}")
            print(f"# {hostname}\n" + "\n\n".join(lines) + "\n")
        else:
            output = _cli_with_retry(
                dev, common.args.show_command, hostname, retry
            )
            # 1回の print で出力し、並列実行時のインターリーブを軽減
            print(f"# {hostname}\n{output.strip()}\n")
        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


def cmd_config(hostname) -> int:
    """Push set command file to device."""
    dev = _open_connection(hostname)
    if dev is None:
        return 1
    try:
        # RPC タイムアウトの設定（CLI > config.ini > デフォルト 120秒）
        timeout = getattr(common.args, "rpc_timeout", None)
        if timeout is None:
            try:
                timeout = int(common.config.get(hostname, "timeout"))
            except (Exception):
                pass
        if timeout is None:
            timeout = 120
        dev.timeout = timeout
        display.print_host_header(hostname)
        result = upgrade.load_config(hostname, dev, common.args.configfile)
        display.print_load_config(result)
        return 0 if result.get("ok") else 1
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


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
        conn = common.connect(hostname)
        if conn["ok"]:
            dev = conn["dev"]
            result["connect"] = {
                "ok": True,
                "message": "connected",
                "error": None,
            }
            # facts 取得は ~10 RPC と重い。--remote でモデル解決が必要な
            # 場合だけ実行する（--connect 単独ではスキップして高速化）。
            if model is None and do_remote:
                try:
                    model = dev.facts.get("model")
                    if model:
                        source = "device"
                except Exception as e:
                    logger.debug(f"{hostname}: facts access failed: {e}")
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
                result["remote"] = upgrade.check_remote_package_by_model(
                    hostname, dev, model
                )
            else:
                reason = "not connected" if dev is None else "model unknown"
                result["remote"] = {
                    "status": "unchecked",
                    "message": reason,
                    "file": None,
                    "cached": False,
                }
    finally:
        if dev is not None:
            try:
                dev.close()
            except (ConnectClosedError, Exception):
                pass

    return result


def _check_local_inventory() -> list[dict]:
    """Inventory-mode ``check --local``: iterate model→file map in ``config.ini``.

    Ignores hostnames entirely — the local firmware map is an attribute
    of the staging server, not the devices. Verifies each model's
    ``<model>.file`` against its ``<model>.hash``, filtered by
    ``--model`` if supplied. Uses ``"DEFAULT"`` as the config section
    so DEFAULT-level ``lpath`` / ``hashalgo`` apply.
    """
    filter_model = getattr(common.args, "check_model", None)
    if filter_model:
        models = [filter_model]
    else:
        models = upgrade.iter_configured_models()

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
        display.print_host_header(hostname)
        result = upgrade.list_remote_path(hostname, dev)
        display.print_list_remote(result)
        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return 1
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass


# --- メイン ---


def main():
    """CLI entry point."""
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
        "--force", action="store_true", help="force execute",
    )
    parent.add_argument(
        "--workers", type=int, default=None,
        help="parallel workers (default: 1 for upgrade, 20 for rsi/check)",
    )
    parent.add_argument(
        "--tags", type=str, default=None,
        help="filter hosts by tags (comma-separated, AND match)",
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
        "-f", "--file", dest="showfile",
        help="path to file containing CLI commands (one per line)",
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
        "-f", "--file", dest="configfile", required=True,
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
             'if omitted: "ping count 3 255.255.255.255 rapid")',
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
    p_config.add_argument("specialhosts", metavar="hostname", nargs="*")

    # check
    p_check = subparsers.add_parser(
        "check",
        parents=[parent],
        help="pre-flight checks (NETCONF reachability, local/remote firmware hash)",
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
    if not hasattr(args, "tags"):
        args.tags = None
    if not hasattr(args, "retry"):
        args.retry = 0
    if not hasattr(args, "rpc_timeout"):
        args.rpc_timeout = None
    if not hasattr(args, "no_confirm"):
        args.no_confirm = False
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

    logger.debug("start")

    cfg_result = common.read_config()
    if not cfg_result["ok"]:
        display.print_read_config_error(cfg_result)
        sys.exit(1)

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

        # --local: staging server 側のファームウェア棚卸し（ホスト非依存）
        if common.args.check_local:
            inventory = _check_local_inventory()
            display.print_check_local_inventory(inventory)
            for row in inventory:
                if row.get("status") in ("bad", "missing", "error"):
                    rc = 1

        # --connect / --remote: ホスト単位でチェック
        if common.args.check_connect or common.args.check_remote:
            if common.args.check_local:
                # 2 つ目のテーブルの前にブランク行
                print("")
            results = common.run_parallel(
                _check_host, targets, max_workers=common.args.workers
            )
            rows = [results[t] for t in targets if t in results]
            display.print_check_table(
                rows,
                show_connect=common.args.check_connect,
                show_local=False,
                show_remote=common.args.check_remote,
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
