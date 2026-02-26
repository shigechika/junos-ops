"""RSI/SCF collection: show configuration and request support information."""

from lxml import etree
import os
from logging import getLogger

from junos_ops import common

logger = getLogger(__name__)


def get_support_information(dev):
    """Run request support information with model-specific timeout.

    :returns: RPC response, or None on failure.
    """
    try:
        if dev.facts["personality"] == "SRX_BRANCH":
            # SRX3xx series is SLOW
            timeout = 1200
        elif dev.facts["model"] == "EX2300-24T":
            timeout = 1200
        elif len(dev.facts["model_info"]) >= 2:
            # Virtual Chassis is more SLOW
            timeout = 1800
            if dev.facts["model"] == "QFX5110-48S-4C":
                # QFX5110-48S-4C is most SLOW
                timeout = 2400
        else:
            timeout = 600

        logger.debug(f"get_support_information: {dev.facts['hostname']} timeout={timeout}")

        if dev.facts.get("srx_cluster") == "True":
            rpc = dev.rpc.get_support_information(
                {"format": "text"}, dev_timeout=timeout, node="primary"
            )
        else:
            rpc = dev.rpc.get_support_information(
                {"format": "text"}, dev_timeout=timeout
            )
        return rpc
    except Exception as e:
        logger.error(f"get_support_information: {e}")
        return None


def cmd_rsi(hostname) -> int:
    """Collect SCF and RSI for a single host and write to files."""
    logger.debug(f"cmd_rsi: {hostname} start")
    print(f"# {hostname}")

    err, dev = common.connect(hostname)
    if err or dev is None:
        return 1

    rsi_dir = os.path.expanduser(
        common.config.get(hostname, "RSI_DIR", fallback="./"))

    try:
        # show configuration → SCF ファイル
        display_style = common.config.get(hostname, "DISPLAY_STYLE",
                                          fallback="display set")
        if display_style:
            scf_cmd = f"show configuration | {display_style}"
        else:
            scf_cmd = "show configuration"
        output_str = dev.cli(scf_cmd)
        scf_path = f"{rsi_dir}{hostname}.SCF"
        with open(scf_path, mode="w") as f:
            f.write(output_str.strip())
        print(f"  {hostname}.SCF done")

        # request support information → RSI ファイル
        rpc = get_support_information(dev)
        if rpc is None:
            logger.error(f"{hostname}: get_support_information failed")
            return 2

        output_str = etree.tostring(rpc, encoding="unicode", method="text")
        rsi_path = f"{rsi_dir}{hostname}.RSI"
        with open(rsi_path, mode="w") as f:
            f.write(output_str.strip())
        print(f"  {hostname}.RSI done")

        return 0
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return 1
    finally:
        try:
            dev.close()
        except Exception:
            pass
