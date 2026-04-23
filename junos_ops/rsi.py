"""RSI/SCF collection: show configuration and request support information."""

from lxml import etree
import os
from logging import getLogger

from jnpr.junos.exception import ConnectClosedError

from junos_ops import common

logger = getLogger(__name__)


def get_support_information(dev) -> dict:
    """Run ``request support information`` with a model-specific timeout.

    :return: dict with keys:

        - ``ok`` (bool): True iff the RPC returned without raising.
        - ``rpc`` (lxml Element | None): raw RPC response on success.
          NOT JSON-serializable; callers that serialize should strip it.
        - ``timeout`` (int): the timeout value selected for this model.
        - ``node`` (str | None): ``"primary"`` when targetting an SRX
          cluster primary node, else None.
        - ``error`` (str | None): exception class name on failure.
        - ``error_message`` (str | None): full error message.

    Does not print.
    """
    # model-specific timeout selection
    try:
        if dev.facts["personality"] == "SRX_BRANCH":
            timeout = 2400  # SRX300/320/340/345 are very slow; 1200 was borderline
        elif dev.facts["model"] == "EX2300-24T":
            timeout = 2400  # low-flash EX2300 RSI can exceed 1200s under load
        elif len(dev.facts["model_info"]) >= 2:
            # Virtual Chassis is slower
            timeout = 1800
            if dev.facts["model"] == "QFX5110-48S-4C":
                timeout = 2400  # matches the slowest tier (SRX_BRANCH / EX2300-24T)
        else:
            timeout = 600
    except Exception as e:
        logger.error(f"get_support_information: timeout resolution failed: {e}")
        return {
            "ok": False,
            "rpc": None,
            "timeout": 0,
            "node": None,
            "error": type(e).__name__,
            "error_message": str(e),
        }

    logger.debug(
        f"get_support_information: {dev.facts.get('hostname')} timeout={timeout}"
    )

    node = None
    try:
        if dev.facts.get("srx_cluster") == "True":
            node = "primary"
            rpc = dev.rpc.get_support_information(
                {"format": "text"}, dev_timeout=timeout, node=node,
            )
        else:
            rpc = dev.rpc.get_support_information(
                {"format": "text"}, dev_timeout=timeout,
            )
    except Exception as e:
        logger.error(f"get_support_information: {e}")
        return {
            "ok": False,
            "rpc": None,
            "timeout": timeout,
            "node": node,
            "error": type(e).__name__,
            "error_message": str(e),
        }
    return {
        "ok": True,
        "rpc": rpc,
        "timeout": timeout,
        "node": node,
        "error": None,
        "error_message": None,
    }


def collect_rsi(hostname, dev) -> dict:
    """Collect SCF (``show configuration``) and RSI for a single host.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool)
        - ``scf`` (dict | None): ``{"path", "bytes", "command"}`` when
          SCF was written, None if it failed before writing.
        - ``rsi`` (dict | None): ``{"path", "bytes"}`` when RSI was
          written.
        - ``rsi_dir`` (str): output directory used.
        - ``error`` (str | None): short error identifier (``"scf"``,
          ``"rsi"``, exception class name, etc.).
        - ``error_message`` (str | None): full error detail.

    Does not print. Writes two files to ``rsi_dir``: ``{hostname}.SCF``
    and ``{hostname}.RSI``.
    """
    result = {
        "hostname": hostname,
        "ok": False,
        "scf": None,
        "rsi": None,
        "rsi_dir": "",
        "error": None,
        "error_message": None,
    }

    rsi_dir = os.path.expanduser(
        common.config.get(hostname, "RSI_DIR", fallback="./")
    )
    result["rsi_dir"] = rsi_dir

    # SCF: show configuration [| display set]
    try:
        display_style = common.config.get(
            hostname, "DISPLAY_STYLE", fallback="display set"
        )
        scf_cmd = (
            f"show configuration | {display_style}"
            if display_style
            else "show configuration"
        )
        output_str = dev.cli(scf_cmd)
        scf_path = f"{rsi_dir}{hostname}.SCF"
        body = output_str.strip()
        with open(scf_path, mode="w") as f:
            f.write(body)
        result["scf"] = {
            "path": scf_path,
            "bytes": len(body),
            "command": scf_cmd,
        }
    except Exception as e:
        result["error"] = "scf"
        result["error_message"] = str(e)
        logger.error(f"{hostname}: SCF collection failed: {e}")
        return result

    # RSI: request support information
    rsi_result = get_support_information(dev)
    if not rsi_result["ok"]:
        result["error"] = "rsi_rpc"
        result["error_message"] = rsi_result["error_message"]
        return result
    try:
        output_str = etree.tostring(
            rsi_result["rpc"], encoding="unicode", method="text"
        )
        rsi_path = f"{rsi_dir}{hostname}.RSI"
        body = output_str.strip()
        with open(rsi_path, mode="w") as f:
            f.write(body)
        result["rsi"] = {"path": rsi_path, "bytes": len(body)}
    except Exception as e:
        result["error"] = "rsi_write"
        result["error_message"] = str(e)
        logger.error(f"{hostname}: RSI write failed: {e}")
        return result

    result["ok"] = True
    return result


def cmd_rsi(hostname) -> int:
    """Collect SCF and RSI for a single host (CLI entry)."""
    logger.debug(f"cmd_rsi: {hostname} start")
    from junos_ops import display

    display.print_host_header(hostname)

    conn = common.connect(hostname)
    if not conn["ok"]:
        display.print_connect_error(conn)
        return 1
    dev = conn["dev"]
    try:
        result = collect_rsi(hostname, dev)
        display.print_rsi(result)
        return 0 if result["ok"] else (2 if result["error"] == "rsi_rpc" else 1)
    finally:
        try:
            dev.close()
        except (ConnectClosedError, Exception):
            pass
