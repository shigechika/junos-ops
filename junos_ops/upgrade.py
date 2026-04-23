"""Package operations: copy, install, rollback, reboot, and version management."""

from looseversion import LooseVersion
from jnpr.junos.exception import (
    ConnectError,
    RpcError,
    RpcTimeoutError,
)
from jnpr.junos.utils.config import Config
from jnpr.junos.utils.fs import FS
from jnpr.junos.utils.sw import SW
from lxml import etree
from ncclient.operations.errors import TimeoutExpiredError
import argparse
import datetime
import hashlib
import os
import re
import logging
from logging import getLogger

from junos_ops import common

logger = getLogger(__name__)


def delete_snapshots(dev) -> dict:
    """Delete all snapshots on EX/QFX series to free disk space.

    :return: dict with keys:

        - ``applied`` (bool): False when the device is not a switch
          (no-op); True when a snapshot delete RPC was attempted
          (including dry-run and non-fatal exceptions).
        - ``ok`` (bool): overall success from the caller's perspective.
          Non-switches and non-fatal RPC exceptions are both reported as
          ``ok=True`` because the legacy implementation treated them as
          success too.
        - ``dry_run`` (bool)
        - ``message`` (str | None)
        - ``error`` (str | None): exception class name if the RPC raised,
          else None.

    Does not print.
    """
    if dev.facts.get("personality") != "SWITCH":
        return {
            "applied": False,
            "ok": True,
            "dry_run": common.args.dry_run,
            "message": None,
            "error": None,
        }

    if common.args.dry_run:
        return {
            "applied": True,
            "ok": True,
            "dry_run": True,
            "message": "dry-run: request system snapshot delete *",
            "error": None,
        }

    try:
        # Send <request-snapshot><delete>*</delete></request-snapshot> as a
        # positional dict, the same idiom used for request_package_rollback
        # elsewhere in this file. The earlier ``delete="*"`` kwarg form was
        # coerced to bool True somewhere in the PyEZ/ncclient XML builder and
        # failed with "Type 'bool' cannot be serialized".
        rpc = dev.rpc.request_snapshot(
            {"delete": "*"}, dev_timeout=60
        )
        xml_str = etree.tostring(rpc, encoding="unicode")
        logger.debug(f"delete_snapshots: {xml_str}")
        return {
            "applied": True,
            "ok": True,
            "dry_run": False,
            "message": "copy: snapshot delete successful",
            "error": None,
        }
    except Exception as e:
        err_name = type(e).__name__
        logger.warning(f"snapshot delete: {err_name}: {e}")
        return {
            "applied": True,
            "ok": True,  # legacy: non-fatal skip
            "dry_run": False,
            "message": f"copy: snapshot delete skipped ({err_name}: {e})",
            "error": err_name,
        }


def copy(hostname, dev) -> dict:
    """Copy package to remote device via SCP with checksum verification.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): overall success. True on successful copy, on
          dry-run, and when skipped because the target version is already
          running or the file is already present remotely.
        - ``skipped`` (bool): True when pre-flight checks determined the
          copy is unnecessary.
        - ``skip_reason`` (str | None): ``"already_running"`` |
          ``"already_copied"`` | None.
        - ``dry_run`` (bool)
        - ``local_file`` (str | None): resolved local package path (None
          if skipped before resolution).
        - ``remote_path`` (str)
        - ``checksum_algo`` (str)
        - ``storage_cleanup`` (dict | None): result of
          ``request-system-storage-cleanup``.
        - ``snapshot_delete`` (dict | None): result of
          :func:`delete_snapshots`.
        - ``steps`` (list[dict]): chronological per-action progress
          entries, each with at minimum an ``action`` and ``message``.
        - ``error`` (str | None): short error identifier when ``ok`` is
          False.

    Does not print. Progress is conveyed via the ``steps`` list for the
    display layer.

    Note: the nested ``check_running_package`` / ``check_remote_package``
    helpers still print to stdout in the current refactor step; that is
    cleaned up in Phase 6.
    """
    logger.debug("copy: start")
    steps: list[dict] = []
    result = {
        "hostname": hostname,
        "ok": False,
        "skipped": False,
        "skip_reason": None,
        "dry_run": common.args.dry_run,
        "local_file": None,
        "remote_path": common.config.get(hostname, "rpath"),
        "checksum_algo": common.config.get(hostname, "hashalgo"),
        "storage_cleanup": None,
        "snapshot_delete": None,
        "steps": steps,
        "error": None,
    }

    # pre-flight: skip entirely if the target version is already running.
    if not common.args.force:
        if check_running_package(hostname, dev)["match"]:
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "already_running"
            steps.append({
                "action": "skip",
                "ok": True,
                "message": "Already Running, COPY Skip.",
            })
            return result

    # request-system-storage-cleanup BEFORE the remote-package check — low-flash
    # devices (EX2300/EX3400) can fill up when the subsequent install extracts
    # the tgz contents, so we want the cleanup to run even on an otherwise
    # idempotent re-run. Note that ``storage cleanup`` also sweeps /var/tmp,
    # which is exactly where our staged package lives; hence the remote check
    # has to happen *after* the cleanup so that a swept-away package gets
    # re-copied instead of failing pkgadd with "cannot find: /var/tmp/...".
    # SRX_BRANCH hosts happened to get clean space from the forced rollback;
    # EX hosts did not and routinely hit "insufficient space" during pkgadd.
    cleanup: dict = {
        "ok": False,
        "dry_run": common.args.dry_run,
        "message": None,
        "error": None,
    }
    if common.args.dry_run:
        cleanup["ok"] = True
        cleanup["message"] = "dry-run: request system storage cleanup"
    else:
        try:
            rpc = dev.rpc.request_system_storage_cleanup(
                no_confirm=True, dev_timeout=60
            )
            xml_str = etree.tostring(rpc, encoding="unicode")
            logger.debug(f"copy: request-system-storage-cleanup={xml_str}")
            if xml_str.find("<success/>") >= 0:
                cleanup["ok"] = True
                cleanup["message"] = "copy: system storage cleanup successful"
            else:
                cleanup["ok"] = False
                cleanup["message"] = "copy: system storage cleanup failed"
        except Exception as e:
            err_name = type(e).__name__
            cleanup["ok"] = False
            cleanup["error"] = err_name
            cleanup["message"] = (
                f"system storage cleanup failure caused by {err_name}: {e}"
            )
    result["storage_cleanup"] = cleanup
    steps.append({"action": "storage_cleanup", **cleanup})
    if not cleanup["ok"]:
        result["error"] = "storage_cleanup_failed"
        return result

    # storage cleanup sweeps /var/tmp, which is exactly where our staged
    # package lives. Drop any cached checksum for this hostname/package so
    # that the remote check below re-verifies against the real device state
    # instead of trusting a checksum that was valid before the cleanup.
    if not common.args.dry_run:
        clear_hashcache(
            hostname, get_model_file(hostname, dev.facts["model"])
        )

    # EX/QFX: snapshot delete to free disk
    snap = delete_snapshots(dev)
    result["snapshot_delete"] = snap
    if snap.get("applied"):
        steps.append({"action": "snapshot_delete", **snap})

    # Remote-package check AFTER cleanup so a swept-away package triggers
    # a fresh copy instead of a failed pkgadd.
    if not common.args.force:
        remote_check = check_remote_package(hostname, dev)
        # When the remote copy is stale (bad) or missing we are about to
        # re-copy, so the "BAD. COPY AGAIN!" / "is not found." wording is
        # rewritten to a forward-looking message instead of quoting the
        # pre-copy state (which reads as a failure once the copy succeeds).
        rc_status = remote_check["status"]
        file = remote_check.get("file") or ""
        if rc_status == "bad":
            step_msg = (
                f"  - remote package: {file} exists but checksum mismatch; "
                "overwriting"
            )
        elif rc_status == "missing":
            step_msg = f"  - remote package: {file} not present; copying"
        else:
            step_msg = remote_check["message"]
        steps.append({
            "action": "remote_check",
            "ok": rc_status == "ok",
            "status": rc_status,
            "message": step_msg,
        })
        if rc_status == "ok":
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "already_copied"
            steps.append({
                "action": "skip",
                "ok": True,
                "message": "remote package is already copied successfully",
            })
            return result

    # scp
    file = get_model_file(hostname, dev.facts["model"])
    local_file = get_local_path(hostname, file)
    result["local_file"] = local_file

    if common.args.dry_run:
        msg = "dry-run: scp(checksum:%s) %s %s:%s" % (
            result["checksum_algo"], local_file, hostname, result["remote_path"],
        )
        steps.append({
            "action": "scp",
            "ok": True,
            "dry_run": True,
            "message": msg,
            "error": None,
        })
        result["ok"] = True
        logger.debug("copy: end (dry-run)")
        return result

    try:
        sw = SW(dev)
        success = sw.safe_copy(
            local_file,
            remote_path=result["remote_path"],
            progress=True,
            cleanfs=True,
            cleanfs_timeout=300,
            checksum=get_model_hash(hostname, dev.facts["model"]),
            checksum_timeout=1200,
            checksum_algorithm=result["checksum_algo"],
            force_copy=common.args.force,
        )
        if success:
            logger.debug("copy: successful")
            steps.append({
                "action": "scp",
                "ok": True,
                "dry_run": False,
                "message": "copy: scp successful",
                "error": None,
            })
            result["ok"] = True
        else:
            logger.debug("copy: failed")
            steps.append({
                "action": "scp",
                "ok": False,
                "dry_run": False,
                "message": "copy: scp failed",
                "error": "scp_failed",
            })
            result["error"] = "scp_failed"
    except TimeoutExpiredError as e:
        msg = f"Copy failure caused by TimeoutExpiredError: {e}"
        steps.append({"action": "scp", "ok": False, "message": msg, "error": "TimeoutExpiredError"})
        result["error"] = "TimeoutExpiredError"
    except RpcTimeoutError as e:
        msg = f"Copy failure caused by RpcTimeoutError: {e}"
        steps.append({"action": "scp", "ok": False, "message": msg, "error": "RpcTimeoutError"})
        result["error"] = "RpcTimeoutError"
    except Exception as e:
        msg = str(e)
        err_name = type(e).__name__
        steps.append({"action": "scp", "ok": False, "message": msg, "error": err_name})
        result["error"] = err_name

    logger.debug(f"copy: end ok={result['ok']}")
    return result


def rollback(hostname, dev) -> dict:
    """Rollback to the previously installed package version.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): True on success (or dry-run).
        - ``dry_run`` (bool)
        - ``rpc_output`` (str | None): full XML/text response from
          ``request-package-rollback`` on success or when the RPC
          returned a recognisable failure response.
        - ``message`` (str): human-readable summary.
        - ``error`` (str | None): exception class name on exception,
          ``"unrecognized_response"`` if the RPC completed but returned
          text the parser couldn't classify as success.

    Does not print.
    """
    result = {
        "hostname": hostname,
        "ok": False,
        "dry_run": common.args.dry_run,
        "rpc_output": None,
        "message": "",
        "error": None,
    }
    if common.args.dry_run:
        result["ok"] = True
        result["message"] = "dry-run: request system software rollback"
        return result
    try:
        rpc = dev.rpc.request_package_rollback({"format": "text"}, dev_timeout=120)
        xml_str = etree.tostring(rpc, encoding="unicode")
        logger.debug(f"rollback: rpc={rpc} xml_str={xml_str}")
        result["rpc_output"] = xml_str
        if (
            xml_str.find("Deleting bootstrap installer") >= 0  # MX
            or xml_str.find("NOTICE: The 'pending' set has been removed") >= 0  # EX
            or xml_str.find("will become active at next reboot") >= 0  # SRX3xx
            or xml_str.find("Rollback of staged upgrade succeeded") >= 0  # SRX1500
            or xml_str.find("There is NO image for ROLLBACK") >= 0  # SRX4600
        ):
            result["ok"] = True
            result["message"] = (
                f"rollback: request system software rollback successful:\n{xml_str}"
            )
        else:
            result["error"] = "unrecognized_response"
            result["message"] = (
                f"rollback: request system software rollback failed:\n{xml_str}"
            )
    except RpcTimeoutError as e:
        result["error"] = "RpcTimeoutError"
        result["message"] = (
            f"request system software rollback failure caused by RpcTimeoutError: {e}"
        )
    except RpcError as e:
        result["error"] = "RpcError"
        result["message"] = (
            f"request system software rollback failure caused by RpcError: {e}"
        )
    except Exception as e:
        result["error"] = type(e).__name__
        result["message"] = str(e)
    return result


def clear_reboot(dev) -> dict:
    """Clear any scheduled reboot on the device.

    :return: dict with keys:

        - ``ok`` (bool): True on success (including dry-run).
        - ``dry_run`` (bool)
        - ``message`` (str): legacy human-readable summary line.
        - ``error`` (str | None): exception class name on failure.

    Does not print.
    """
    result = {
        "ok": False,
        "dry_run": common.args.dry_run,
        "message": "",
        "error": None,
    }
    if common.args.dry_run:
        result["ok"] = True
        result["message"] = "\tdry-run: clear system reboot"
        return result
    try:
        rpc = dev.rpc.clear_reboot({"format": "text"})
        xml_str = etree.tostring(rpc, encoding="unicode")
        logger.debug(f"{rpc=} {xml_str=}")
        if (
            xml_str.find("No shutdown/reboot scheduled.") >= 0
            or xml_str.find("Terminating...") >= 0
        ):
            logger.debug("clear reboot schedule successful")
            result["ok"] = True
            result["message"] = "\tclear reboot schedule successful"
        else:
            logger.debug("clear reboot schedule failed")
            result["message"] = "\tclear reboot schedule failed"
    except RpcTimeoutError as e:
        result["error"] = "RpcTimeoutError"
        result["message"] = f"\tclear reboot failure: RpcTimeoutError: {e}"
        logger.error(f"Clear reboot failure caused by RpcTimeoutError: {e}")
    except RpcError as e:
        result["error"] = "RpcError"
        result["message"] = f"\tclear reboot failure: RpcError: {e}"
        logger.error(f"Clear reboot failure caused by RpcError: {e}")
    except Exception as e:
        result["error"] = type(e).__name__
        result["message"] = f"\tclear reboot failure: {type(e).__name__}: {e}"
        logger.error(e)
    return result


def install(hostname, dev) -> dict:
    """Install package with pre-flight checks.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): overall success.
        - ``skipped`` (bool): True when pre-flight determined no action
          was needed (already running or pending already ahead).
        - ``skip_reason`` (str | None): ``"already_running"`` |
          ``"pending_ge_planning"`` | ``"remote_missing"`` | None.
        - ``dry_run`` (bool)
        - ``pending`` (str | None): current pending version.
        - ``planning`` (str | None): planned version (from package name).
        - ``compare`` (int | None): result of
          :func:`compare_version` (1 / 0 / -1 / None).
        - ``rollback_result`` (dict | None): nested
          :func:`rollback` result when a rollback was performed.
        - ``copy_result`` (dict | None): nested :func:`copy` result.
        - ``rescue_save`` (dict | None): ``{ok, message, error}`` for
          ``request system configuration rescue save``.
        - ``install_message`` (str | None): message from PyEZ
          ``SW.install`` or the dry-run equivalent.
        - ``steps`` (list[dict]): chronological per-action progress for
          the display layer.
        - ``error`` (str | None): short identifier for the first
          fatal failure.

    Does not print.
    """
    logger.debug("install: start")
    steps: list[dict] = []
    result: dict = {
        "hostname": hostname,
        "ok": False,
        "skipped": False,
        "skip_reason": None,
        "dry_run": common.args.dry_run,
        "pending": None,
        "planning": None,
        "compare": None,
        "rollback_result": None,
        "copy_result": None,
        "rescue_save": None,
        "install_message": None,
        "steps": steps,
        "error": None,
    }

    # pre-flight: already running?
    if not common.args.force:
        if check_running_package(hostname, dev)["match"]:
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "already_running"
            steps.append({"action": "skip", "message": "Already Running, INSTALL Skip."})
            return result

    pending = get_pending_version(hostname, dev)
    result["pending"] = pending
    logger.debug(f"{pending=}")
    if pending is not None:
        planning = get_planning_version(hostname, dev)
        result["planning"] = planning
        logger.debug(f"{planning=}")
        cmp_ret = compare_version(pending, planning)
        result["compare"] = cmp_ret
        logger.debug(f"install: compare_version={cmp_ret}")
        if cmp_ret == 1:
            steps.append({
                "action": "compare",
                "message": f"\t{pending=} > {planning=} : No need install.",
            })
        elif cmp_ret == -1:
            steps.append({
                "action": "compare",
                "message": f"\t{pending=} < {planning=} : NEED INSTALL.",
            })
        elif cmp_ret == 0:
            steps.append({
                "action": "compare",
                "message": f"\t{pending=} = {planning=} : No need install.",
            })

        if cmp_ret in (0, 1) and not common.args.force:
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "pending_ge_planning"
            return result

        rollback_result = rollback(hostname, dev)
        result["rollback_result"] = rollback_result
        if not rollback_result.get("ok") and not common.args.force:
            result["error"] = "rollback_failed"
            return result

    # EX series deletes remote package after install; remote check first.
    # Use the active subcommand to decide whether to skip or fail-fast.
    # (Legacy ``args.copy`` / ``args.update`` / ``args.install`` flags were
    # removed with ``process_host`` in v0.14.0.)
    subcmd = getattr(common.args, "subcommand", None)
    if common.args.dry_run and subcmd == "upgrade":
        # dry-run upgrade: copy() will also be dry-run, remote check moot.
        steps.append({
            "action": "remote_check",
            "message": "dry-run: skip remote package check",
        })
    else:
        remote_check = check_remote_package(hostname, dev)
        steps.append({
            "action": "remote_check",
            "ok": remote_check["status"] == "ok",
            "status": remote_check["status"],
            "message": remote_check["message"],
        })
        if remote_check["status"] != "ok" and subcmd == "install":
            # install-only mode: fail fast when the remote package is missing.
            # The step message below carries this to the display layer; a
            # parallel logger.info would double-print on stdout.
            result["skip_reason"] = "remote_missing"
            result["error"] = "remote_missing"
            steps.append({
                "action": "remote_missing",
                "message": "remote package file not found. "
                           "Please consider --copy before --install",
            })
            return result

    copy_result = copy(hostname, dev)
    result["copy_result"] = copy_result
    if not copy_result.get("ok"):
        result["error"] = "copy_failed"
        return result

    clear_result = clear_reboot(dev)
    steps.append({"action": "clear_reboot", **clear_result})
    if not clear_result["ok"]:
        result["error"] = "clear_reboot_failed"
        return result

    # rescue config save
    rescue_save: dict = {"ok": False, "dry_run": common.args.dry_run, "message": None, "error": None}
    if common.args.dry_run:
        rescue_save["ok"] = True
        rescue_save["message"] = "dry-run: request system configuration rescue save"
    else:
        cu = Config(dev)
        try:
            saved = cu.rescue("save")
            if saved:
                rescue_save["ok"] = True
                rescue_save["message"] = "install: rescue config save successful"
            else:
                rescue_save["message"] = "install: rescue config save failed"
        except ValueError as e:
            rescue_save["error"] = "ValueError"
            rescue_save["message"] = f"wrong rescue action {e}"
        except Exception as e:
            rescue_save["error"] = type(e).__name__
            rescue_save["message"] = str(e)
    result["rescue_save"] = rescue_save
    steps.append({"action": "rescue_save", **rescue_save})
    if not rescue_save["ok"]:
        result["error"] = "rescue_save_failed"
        return result

    # request system software add ...
    if common.args.dry_run:
        msg = "dry-run: request system software add %s/%s" % (
            common.config.get(hostname, "rpath"),
            get_model_file(hostname, dev.facts["model"]),
        )
        result["install_message"] = msg
        result["ok"] = True
        steps.append({"action": "sw_install", "ok": True, "dry_run": True, "message": msg})
        return result

    sw = SW(dev)
    try:
        status, msg = sw.install(
            get_model_file(hostname, dev.facts["model"]),
            remote_path=common.config.get(hostname, "rpath"),
            progress=True,
            validate=True,
            cleanfs=True,
            no_copy=True,
            issu=False,
            nssu=False,
            timeout=2400,
            cleanfs_timeout=300,
            checksum=get_model_hash(hostname, dev.facts["model"]),
            checksum_timeout=1200,
            checksum_algorithm=common.config.get(hostname, "hashalgo"),
            force_copy=common.args.force,
            all_re=True,
        )
    finally:
        del sw
    logger.debug(f"{msg=}")
    result["install_message"] = msg
    if status:
        logger.debug("install successful")
        result["ok"] = True
        steps.append({"action": "sw_install", "ok": True, "message": msg})
    else:
        logger.debug("install failed")
        result["error"] = "sw_install_failed"
        steps.append({"action": "sw_install", "ok": False, "message": msg})

    logger.debug(f"install: end ok={result['ok']}")
    return result


def get_model_file(hostname, model):
    """Look up package filename for a device model."""
    try:
        return common.config.get(hostname, model.lower() + ".file")
    except Exception as e:
        logger.error(f"{hostname}: {model.lower()}.file not found in recipe: {e}")
        raise


def get_local_path(hostname, filename):
    """Build local file path by joining lpath with filename.

    Supports ``~`` expansion (e.g., ``~/firmware``), consistent with
    the ``sshkey`` setting in :func:`common.connect`.

    :param hostname: hostname (config section key)
    :param filename: package filename from get_model_file()
    :return: full local path (lpath/filename), or filename if lpath is not set
    """
    try:
        lpath = common.config.get(hostname, "lpath")
    except Exception:
        return filename
    if not lpath:
        return filename
    return os.path.join(os.path.expanduser(lpath), filename)


def get_model_hash(hostname, model):
    """Look up expected checksum for a device model."""
    try:
        return common.config.get(hostname, model.lower() + ".hash")
    except Exception as e:
        logger.error(f"{hostname}: {model.lower()}.hash not found in recipe: {e}")
        raise


def get_hashcache(hostname, file):
    """Get cached checksum value (thread-safe)."""
    with common.config_lock:
        if common.config.has_section(hostname) is False:
            return None
        if common.config.has_option(hostname, file + "hashcache"):
            hashcache = common.config.get(hostname, file + "hashcache")
        else:
            hashcache = None
        return hashcache


def set_hashcache(hostname, file, value):
    """Set cached checksum value (thread-safe)."""
    with common.config_lock:
        if common.config.has_section(hostname) is False:
            # "localhost"
            common.config.add_section(hostname)
        common.config.set(hostname, file + "hashcache", value)


def clear_hashcache(hostname, file) -> None:
    """Drop the cached checksum entry for ``hostname``/``file`` (thread-safe).

    Call this whenever something may have deleted the file out from under the
    cache — notably after ``request system storage cleanup`` sweeps ``/var/tmp``.
    Without this, :func:`check_remote_package_by_model` would keep reporting
    ``checksum(cache) is OK`` even though the file is gone, and the subsequent
    ``pkgadd`` would fail with ``cannot find: /var/tmp/...``.
    """
    with common.config_lock:
        if not common.config.has_section(hostname):
            return
        common.config.remove_option(hostname, file + "hashcache")


def iter_configured_models() -> list[str]:
    """Enumerate unique model names with ``<model>.file`` in the DEFAULT section.

    Used by ``check --local`` inventory mode to walk the firmware map
    without needing a host list. Returns lowercase names (configparser
    lowercases keys); display rendering preserves this.
    """
    models: set[str] = set()
    for key in common.config.defaults():
        if key.endswith(".file"):
            models.add(key[: -len(".file")])
    return sorted(models)


def _compute_local_checksum(path: str, algo: str) -> str:
    """Compute a file checksum using hashlib (no PyEZ/device required).

    :param path: absolute or expanded local path.
    :param algo: algorithm name accepted by :func:`hashlib.new`
        (e.g. ``"md5"``, ``"sha1"``, ``"sha256"``, ``"sha512"``).
    :raises FileNotFoundError: the file does not exist.
    :raises ValueError: algorithm name not recognized by hashlib.
    """
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_local_package_by_model(hostname: str, model: str) -> dict:
    """Device-less local package checksum verification.

    Resolves the expected filename / hash from ``config.ini`` using the
    explicit ``model`` argument, then verifies the file on the staging
    server's filesystem via :mod:`hashlib`. No NETCONF / PyEZ device
    connection is required, so this is suitable for bulk pre-flight
    checks on many hosts from a staging server.

    :return: same dict schema as :func:`check_local_package`.

    Does not print.
    """
    logger.debug("check_local_package_by_model: start")
    file = get_model_file(hostname, model)
    local_file = get_local_path(hostname, file)
    pkg_hash = get_model_hash(hostname, model)
    algo = common.config.get(hostname, "hashalgo")
    result = {
        "hostname": hostname,
        "file": file,
        "local_file": local_file,
        "algo": algo,
        "expected_hash": pkg_hash,
        "actual_hash": None,
        "status": "unchecked",
        "cached": False,
        "message": "",
        "error": None,
    }
    if len(file) == 0 or len(pkg_hash) == 0:
        return result

    # Hash cache hit: short-circuit the I/O.
    if get_hashcache("localhost", file) == pkg_hash:
        result["status"] = "ok"
        result["cached"] = True
        result["actual_hash"] = pkg_hash
        result["message"] = (
            f"  - local package: {local_file} is found. checksum(cache) is OK."
        )
        return result

    try:
        val = _compute_local_checksum(local_file, algo)
        result["actual_hash"] = val
        if val == pkg_hash:
            set_hashcache("localhost", file, val)
            result["status"] = "ok"
            result["message"] = (
                f"  - local package: {local_file} is found. checksum is OK."
            )
        else:
            result["status"] = "bad"
            result["message"] = (
                f"  - local package: {local_file} is found. "
                f"checksum is BAD. COPY AGAIN!"
            )
    except FileNotFoundError as e:
        result["status"] = "missing"
        result["message"] = f"  - local package: {local_file} is not found."
        logger.debug(e)
    except Exception as e:
        result["status"] = "error"
        result["error"] = type(e).__name__
        result["message"] = (
            f"  - local package: {local_file} checksum error: {e}"
        )
        logger.error(e)
    return result


def check_local_package(hostname, dev) -> dict:
    """Check the local package file's checksum against the expected value.

    Thin wrapper that resolves the device model from ``dev.facts`` then
    delegates to :func:`check_local_package_by_model`. Retained for
    backward compatibility with callers that have a live device.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``file`` (str): package filename from config (without path).
        - ``local_file`` (str): full local path resolved via ``lpath``.
        - ``algo`` (str): checksum algorithm name.
        - ``expected_hash`` (str): expected checksum from config.
        - ``actual_hash`` (str | None): checksum actually computed (or
          cached). None when the file could not be hashed.
        - ``status`` (str): one of

          * ``"ok"``      — file present, checksum matches.
          * ``"bad"``     — file present, checksum does not match.
          * ``"missing"`` — file not found on the local filesystem.
          * ``"error"``   — hashing raised an unexpected exception
            (see ``error``).
          * ``"unchecked"`` — the model has no configured package
            mapping (empty file name or hash); no check attempted.

        - ``cached`` (bool): True iff the result was served from the
          in-process hash cache and no disk I/O was performed.
        - ``message`` (str): a human-readable summary line suitable
          for the display layer (legacy single-line format).
        - ``error`` (str | None): exception class name when ``status``
          is ``"error"``.

    Does not print.
    """
    return check_local_package_by_model(hostname, dev.facts["model"])


def check_remote_package_by_model(hostname: str, dev, model: str) -> dict:
    """Verify the remote package checksum using an explicit model.

    Device connection is still required (remote checksum is computed
    by the device itself via NETCONF), but the model is supplied by
    the caller instead of being derived from ``dev.facts["model"]``.
    Useful when the caller has already resolved the model from
    ``config.ini`` or a CLI flag.

    :return: same dict schema as :func:`check_remote_package`.

    Does not print.
    """
    logger.debug("check_remote_package_by_model: start")
    file = get_model_file(hostname, model)
    pkg_hash = get_model_hash(hostname, model)
    algo = common.config.get(hostname, "hashalgo")
    rpath = common.config.get(hostname, "rpath")
    result = {
        "hostname": hostname,
        "file": file,
        "remote_path": rpath,
        "algo": algo,
        "expected_hash": pkg_hash,
        "actual_hash": None,
        "status": "unchecked",
        "cached": False,
        "message": "",
        "error": None,
    }
    if len(file) == 0 or len(pkg_hash) == 0:
        return result

    if get_hashcache(hostname, file) == pkg_hash:
        result["status"] = "ok"
        result["cached"] = True
        result["actual_hash"] = pkg_hash
        result["message"] = (
            f"  - remote package: {file} is found. checksum(cache) is OK."
        )
        return result

    sw = SW(dev)
    try:
        val = sw.remote_checksum(f"{rpath}/{file}", algorithm=algo)
        result["actual_hash"] = val
        if val is None:
            result["status"] = "missing"
            result["message"] = f"  - remote package: {file} is not found."
        elif val == pkg_hash:
            set_hashcache(hostname, file, val)
            result["status"] = "ok"
            result["message"] = (
                f"  - remote package: {file} is found. checksum is OK."
            )
        else:
            result["status"] = "bad"
            result["message"] = (
                f"  - remote package: {file} is found. "
                f"checksum is BAD. COPY AGAIN!"
            )
    except RpcError as e:
        result["status"] = "error"
        result["error"] = "RpcError"
        result["message"] = f"  - remote package: unable to checksum: {e}"
        logger.error(f"Unable to remote checksum: {e}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = type(e).__name__
        result["message"] = f"  - remote package: checksum error: {e}"
        logger.error(e)
    finally:
        del sw
    return result


def check_remote_package(hostname, dev) -> dict:
    """Check the remote package file's checksum against the expected value.

    Thin wrapper that resolves the device model from ``dev.facts`` then
    delegates to :func:`check_remote_package_by_model`.

    :return: dict with the same keys as :func:`check_local_package`,
        minus ``local_file`` and plus ``remote_path``. ``status`` values
        are identical (``"ok"`` / ``"bad"`` / ``"missing"`` / ``"error"``
        / ``"unchecked"``).

    Does not print.
    """
    return check_remote_package_by_model(hostname, dev, dev.facts["model"])


def list_remote_path(hostname, dev) -> dict:
    """List files on the remote device's ``rpath`` directory.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``path`` (str): the directory inspected (``rpath``).
        - ``files`` (list[dict]): one entry per child with keys ``name``,
          ``type`` ("file" | "dir" | ...), ``path``, ``size``, ``owner``,
          ``permissions_text``, ``ts_date``. Unknown fields are None.
        - ``file_count`` (int): total file count reported by PyEZ FS.ls.
        - ``format`` (str): "short" or "long" from ``args.list_format``
          (passed through so display can honour the CLI flag).

    Does not print.
    """
    logger.debug("list_remote_path: start")
    fs = FS(dev)
    rpath = common.config.get(hostname, "rpath")
    dir_info = fs.ls(path=rpath, brief=False)
    raw_files = dir_info.get("files") or {}
    files = []
    for name, entry in raw_files.items():
        files.append({
            "name": name,
            "type": entry.get("type"),
            "path": entry.get("path"),
            "size": entry.get("size"),
            "owner": entry.get("owner"),
            "permissions_text": entry.get("permissions_text"),
            "ts_date": entry.get("ts_date"),
        })
    logger.debug("list_remote_path: end")
    return {
        "hostname": hostname,
        "path": dir_info.get("path"),
        "files": files,
        "file_count": dir_info.get("file_count"),
        "format": getattr(common.args, "list_format", None),
    }


def dry_run(hostname, dev) -> dict:
    """Perform dry-run checks for local and remote packages.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``model`` (str)
        - ``local_file`` (str): full local path of the expected package.
        - ``planning_hash`` (str): expected checksum from config.
        - ``algo`` (str): checksum algorithm name.
        - ``local_package`` (dict): nested :func:`check_local_package`
          result. Inspect ``local_package["status"]`` (``"ok"`` | ...).
        - ``remote_package`` (dict): nested :func:`check_remote_package`
          result.
        - ``ok`` (bool): True iff both nested checks report
          ``status == "ok"``.

    Does not print.
    """
    logger.debug("dry-run: start")
    model = dev.facts["model"]
    file = get_model_file(hostname, model)
    local_file = get_local_path(hostname, file)
    planning_hash = get_model_hash(hostname, model)
    algo = common.config.get(hostname, "hashalgo")
    logger.debug(f"hostname: {dev.facts['hostname']}")
    logger.debug(f"model: {model}")
    logger.debug(f"file: {local_file}")
    logger.debug(f"hash: {planning_hash}")
    logger.debug(f"algo: {algo}")
    local = check_local_package(hostname, dev)
    remote = check_remote_package(hostname, dev)
    logger.debug("dry-run: end")
    return {
        "hostname": hostname,
        "model": model,
        "local_file": local_file,
        "planning_hash": planning_hash,
        "algo": algo,
        "local_package": local,
        "remote_package": remote,
        "ok": (
            local["status"] == "ok" and remote["status"] == "ok"
        ),
    }


def check_running_package(hostname, dev) -> dict:
    """Compare the running Junos version with the planning package file.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``running`` (str): running version from ``dev.facts["version"]``.
        - ``expected_file`` (str): planned package filename for the model.
        - ``match`` (bool): True iff ``running`` appears in
          ``expected_file`` (meaning the device is already on the
          planned version).

    Does not print.
    """
    logger.debug("check_running_package: start")
    ver = dev.facts["version"]
    rever = re.sub(r"\.", r"\\.", ver)
    expected_file = get_model_file(hostname, dev.facts["model"])
    logger.debug(f"check_running_package: ver={ver} rever={rever}")
    m = re.search(rever, expected_file)
    logger.debug(f"check_running_package: m={m}")
    match = m is not None
    logger.debug("check_running_package: end")
    return {
        "hostname": hostname,
        "running": ver,
        "expected_file": expected_file,
        "match": match,
    }


def compare_version(left: str, right: str) -> int | None:
    """compare version left and right

    :param left: version left string, ex 18.4R3-S9.2
    :param right: version right string, ex 18.4R3-S10

    :return:  1 if left  > right
              0 if left == right
             -1 if left  < right
    """
    logger.debug(f"compare_version: left={left}, right={right}.")
    if left is None or right is None:
        return None
    if LooseVersion(left.replace("-S", "00")) > LooseVersion(right.replace("-S", "00")):
        return 1
    if LooseVersion(left.replace("-S", "00")) < LooseVersion(right.replace("-S", "00")):
        return -1
    return 0


def get_pending_version(hostname, dev) -> str:
    """Get pending (staged) version string.

    :returns:
       * ``None`` no pending version.
       * ``str`` pending version string.
    """
    pending = None
    try:
        rpc = dev.rpc.get_software_information({"format": "text"})
        xml_str = etree.tostring(rpc, encoding="unicode")
        logger.debug(
            f"get_pending_version: rpc={rpc} type(xml_str)={type(xml_str)} xml_str={xml_str}"
        )
        if dev.facts["personality"] == "SWITCH":
            logger.debug("get_pending_version: EX/QFX series")
            # Pending: 18.4R3-S10
            m = re.search(r"^Pending:\s(.*)$", xml_str, re.MULTILINE)
            if m is not None:
                pending = m.group(1)
        elif dev.facts["personality"] == "MX":
            logger.debug("get_pending_version: MX series")
            # JUNOS Installation Software [18.4R3-S10]
            m = re.search(
                r"^JUNOS\sInstallation\sSoftware\s\[(.*)\]$", xml_str, re.MULTILINE
            )
            if m is not None:
                pending = m.group(1)
        elif dev.facts["personality"] == "SRX_BRANCH":
            # Dual Partition - SRX300, SRX345
            logger.debug("get_pending_version: SRX_BRANCH series")
            xml = dev.rpc.get_snapshot_information(media="internal")
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "get_snapshot_information: xml=%s",
                    etree.tostring(xml, pretty_print=True).decode(),
                )
            pv = xml.xpath(
                ".//snapshot-medium[contains(., 'primary')]"
                "/following-sibling::software-version[1]"
                "/package/package-version"
            )
            if pv and pv[0].text:
                pending = pv[0].text.strip()
        elif (
            dev.facts["personality"] == "SRX_MIDRANGE"
            or dev.facts["personality"] == "SRX_HIGHEND"
        ):
            # SRX1500, SRX4600
            logger.debug("get_pending_version: SRX_MIDRANGE or SRX_HIGHEND series")
            # show log install
            # upgrade_platform: Staging of /var/tmp/junos-srxentedge-x86-64-20.4R3.8-linux.tgz completed
            # &lt;package-result&gt;0&lt;/package-result&gt;
            try:
                rpc = dev.rpc.get_log({"format": "text"}, filename="install")
                xml_str = etree.tostring(rpc, encoding="unicode")
                logger.debug(
                    f"get_pending_version: rpc={rpc} type(xml_str)={type(xml_str)} xml_str={xml_str}"
                )
                if xml_str is not None:
                    # search from last <output> block
                    start = xml_str.rfind("&lt;output&gt;")
                    m = re.search(
                        r"upgrade_platform: Staging of /var/tmp/.*-(\d{2}\.\d.*\d).*\.tgz completed",
                        xml_str[start:],
                        re.MULTILINE,
                    )
                    if m is not None:
                        pending = m.group(1).strip()
                    m = re.search(
                        r"&lt;package-result&gt;(\d)&lt;/package-result&gt;",
                        xml_str[start:],
                        re.MULTILINE,
                    )
                    if m is not None:
                        if int(m.group(1)) == 0:
                            pass
                        else:
                            pending = None
            except Exception as e:
                logger.error(f"get_pending_version: {e}")
                return None
        else:
            logger.error(f"get_pending_version: unknown personality: {dev.facts}")
            return None
    except RpcTimeoutError as e:
        logger.error(f"get_pending_version: RpcTimeoutError: {e}")
        return None
    except RpcError as e:
        logger.error(f"get_pending_version: RpcError: {e}")
        return None
    except Exception as e:
        logger.error(f"get_pending_version: {e}")
        return None
    return pending


def get_planning_version(hostname, dev) -> str:
    """Extract planning version from package filename."""
    planning = None
    f = get_model_file(hostname, dev.facts["model"])
    m = re.search(r".*-(\d{2}\.\d.*\d).*\.tgz", f)
    if m is not None:
        planning = m.group(1).strip()
    else:
        logger.debug("get_planning_version: planning version is not found")
    return planning


def get_reboot_information(hostname, dev):
    """show system reboot
    :return: halt requested by exadmin at Sun Dec 19 08:30:00 2021
             shutdown requested by exadmin at Sun Dec 12 08:30:00 2021
             reboot requested by exadmin at Sun Dec  5 01:00:00 2021
             No shutdown/reboot scheduled.
    """
    try:
        rpc = dev.rpc.get_reboot_information({"format": "text"})
    except RpcError as e:
        logger.error("Show version failure caused by RpcError:", e)
        return None
    except RpcTimeoutError as e:
        logger.error("Show version failure caused by RpcTimeoutError:", e)
        return None
    except Exception as e:
        logger.error(e)
        return None
    xml_str = etree.tostring(rpc, encoding="unicode")
    logger.debug(xml_str)
    m = re.search(
        r"((halt|shutdown|reboot)\srequested\sby\s.*\sat\s(.*\d)|No\sshutdown\/reboot\sscheduled\.)",
        xml_str,
        re.MULTILINE,
    )
    if m is None:
        return None
    return m.group(1)


def get_commit_information(dev):
    """Get the latest commit information.

    :return: (epoch_seconds, datetime_str, user, client) tuple, or None.
    """
    try:
        xml = dev.rpc.get_commit_information()
    except RpcError as e:
        logger.error(f"get_commit_information: RpcError: {e}")
        return None
    except RpcTimeoutError as e:
        logger.error(f"get_commit_information: RpcTimeoutError: {e}")
        return None
    except Exception as e:
        logger.error(f"get_commit_information: {e}")
        return None

    for elem in xml:
        if elem.tag == "commit-history":
            seq = elem.find("sequence-number")
            if seq is not None and seq.text == "0":
                dt = elem.find("date-time")
                user = elem.find("user")
                client = elem.find("client")
                if dt is not None:
                    epoch = int(dt.get("seconds", "0"))
                    return (epoch, dt.text, user.text if user is not None else "", client.text if client is not None else "")
    return None


def get_pending_install_time(dev):
    """Best-effort epoch seconds when the pending firmware slot was staged.

    Returns the mtime of the newest file under ``/var/sw/pkg/``, which is
    where the JUNOS installer saves the staged package on every platform
    observed so far (EX / QFX / SRX / MX). The embedded config in the
    pending image was captured at this time, so comparing it against the
    latest commit epoch answers the real question that
    :func:`check_and_reinstall` is trying to answer: "does the pending
    image already reflect the running config, or is a re-install needed
    to update the embedded config?"

    :returns: epoch seconds (int), or ``None`` if the directory is empty,
        missing, or the RPC fails. Callers should treat ``None`` as
        "unknown" and fall back to secondary heuristics.
    """
    try:
        xml = dev.rpc.file_list(path="/var/sw/pkg/", detail=True)
    except RpcError as e:
        logger.debug(f"get_pending_install_time: RpcError: {e}")
        return None
    except RpcTimeoutError as e:
        logger.debug(f"get_pending_install_time: RpcTimeoutError: {e}")
        return None
    except Exception as e:
        logger.debug(f"get_pending_install_time: {e}")
        return None

    newest = 0
    for fi in xml.iter("file-information"):
        fd = fi.find("file-date")
        if fd is None:
            continue
        seconds = fd.get("seconds")
        if not seconds:
            continue
        try:
            n = int(seconds)
        except ValueError:
            continue
        if n > newest:
            newest = n
    return newest if newest > 0 else None


def get_rescue_config_time(dev):
    """Get rescue config file modification time.

    :return: epoch_seconds (int), or None if file missing or error.
    """
    try:
        xml = dev.rpc.file_list(path="/config/rescue.conf.gz", detail=True)
    except RpcError as e:
        logger.error(f"get_rescue_config_time: RpcError: {e}")
        return None
    except RpcTimeoutError as e:
        logger.error(f"get_rescue_config_time: RpcTimeoutError: {e}")
        return None
    except Exception as e:
        logger.error(f"get_rescue_config_time: {e}")
        return None

    # ファイルが存在しない場合は <output> にエラーメッセージが入る
    file_info = xml.find(".//file-information")
    if file_info is None:
        return None
    file_date = file_info.find("file-date")
    if file_date is None:
        return None
    seconds = file_date.get("seconds")
    if seconds is None:
        return None
    return int(seconds)


def show_version(hostname, dev) -> dict:
    """Collect version information for a device.

    Gathers running/planning/pending versions, last commit info,
    local/remote package presence, and scheduled reboot (if any).
    Does not print — callers use :func:`junos_ops.display.print_version`
    to render for humans, or consume the dict directly.

    :returns: dict with the following keys:

        - ``hostname`` (str): device hostname from ``dev.facts``
        - ``model`` (str): device model
        - ``running`` (str): currently-running JUNOS version
        - ``planning`` (str | None): version parsed from the package
          filename in config.ini, or ``None`` if ``.file`` is not
          configured for this model
        - ``pending`` (str | None): staged version waiting for reboot
        - ``running_vs_planning`` (int | None): ``compare_version``
          result (-1/0/1), or ``None`` if either side is ``None``
        - ``running_vs_pending`` (int | None): same, for pending
        - ``commit`` (dict | None): ``{"epoch", "datetime", "user",
          "client"}`` from the latest commit, or ``None``
        - ``rescue_config_epoch`` (int | None): mtime of the rescue
          config file, if any
        - ``config_changed_after_install`` (bool): True when a pending
          install exists and the config was modified after the rescue
          config was saved (i.e., a reinstall will run on reboot)
        - ``local_package`` (bool | None): local package file present?
          ``None`` if not checkable
        - ``remote_package`` (bool | None): remote package file present?
        - ``reboot_scheduled`` (str | None): raw phrase from
          ``get_reboot_information``, or ``None``
    """
    logger.debug("start")

    running = dev.facts["version"]

    try:
        planning = get_planning_version(hostname, dev)
    except Exception:
        planning = None

    pending = get_pending_version(hostname, dev)

    commit_info = get_commit_information(dev)
    if commit_info is not None:
        commit_epoch, commit_dt_str, commit_user, commit_client = commit_info
        commit_dict = {
            "epoch": commit_epoch,
            "datetime": commit_dt_str,
            "user": commit_user,
            "client": commit_client,
        }
    else:
        commit_dict = None
        commit_epoch = None

    rescue_epoch = None
    config_changed_after_install = False
    if commit_dict is not None and pending is not None:
        rescue_epoch = get_rescue_config_time(dev)
        if rescue_epoch is None or commit_epoch > rescue_epoch:
            config_changed_after_install = True

    try:
        local = check_local_package(hostname, dev)
    except Exception:
        local = None

    try:
        remote = check_remote_package(hostname, dev)
    except Exception:
        remote = None

    rebooting = get_reboot_information(hostname, dev)

    logger.debug("end")

    return {
        "hostname": dev.facts["hostname"],
        "model": dev.facts["model"],
        "running": running,
        "planning": planning,
        "pending": pending,
        "running_vs_planning": compare_version(running, planning),
        "running_vs_pending": compare_version(running, pending),
        "commit": commit_dict,
        "rescue_config_epoch": rescue_epoch,
        "config_changed_after_install": config_changed_after_install,
        "local_package": local,
        "remote_package": remote,
        "reboot_scheduled": rebooting,
    }


def check_and_reinstall(hostname, dev) -> dict:
    """Pre-reboot drift check against the pending firmware image.

    Does **not** attempt to re-install: JUNOS refuses any install while a
    pending slot is already staged (it responds with
    ``There is already an install pending.`` — the only supported paths
    are ``request system reboot`` or ``request system software rollback``).
    The earlier design of this function tried to refresh the pending
    image's embedded config with ``sw.install(validate=True)``, but that
    pattern has never been possible against an already-pending slot and
    simply failed the reboot operation on platforms without rescue
    config (issue #54, issue #57).

    Instead, this function now diagnoses whether the pending image's
    embedded config is likely to be stale with respect to the running
    config, surfaces a warning if so, and always returns ``ok=True`` so
    the caller can proceed with the reboot. The decision of *how* to
    refresh the pending image (rollback + re-upgrade) is left to the
    operator.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): always True; the function no longer does
          anything that can fail. ``reboot`` should always proceed.
        - ``reinstalled`` (bool): always False (kept for result-dict
          stability; no install is attempted).
        - ``skipped`` (bool): always True; re-install is always skipped.
        - ``skip_reason`` (str): ``"no_pending"`` | ``"no_commit_info"``
          | ``"pending_current"`` | ``"config_unchanged"`` |
          ``"drift_detected"`` | ``"cannot_verify"``.

          * ``"pending_current"`` — primary, authoritative skip: the
            pending image's install time (``/var/sw/pkg/`` mtime) is no
            older than the latest commit, so the embedded config is
            current.
          * ``"config_unchanged"`` — legacy fallback when
            ``pending_install_epoch`` cannot be determined but
            ``rescue_epoch`` is and no newer commit exists.
          * ``"drift_detected"`` — commit happened after the pending
            install; the embedded config is provably stale. A warning
            step explains the situation.
          * ``"cannot_verify"`` — neither ``pending_install_epoch`` nor
            ``rescue_epoch`` is available. Softer warning.
        - ``dry_run`` (bool)
        - ``commit`` (dict | None): ``{epoch, datetime, user, client}``
          of the most recent commit, when available.
        - ``rescue_epoch`` (int | None): mtime of the rescue config.
        - ``pending_install_epoch`` (int | None): best-effort epoch of
          the most recent file under ``/var/sw/pkg/``, used as a proxy
          for when the pending firmware was staged.
        - ``drift_detected`` (bool): True when commit is provably newer
          than the pending install.
        - ``steps`` (list[dict]): chronological progress for display.
        - ``error`` (str | None): always None.

    Does not print.
    """
    steps: list[dict] = []
    result: dict = {
        "hostname": hostname,
        "ok": True,
        "reinstalled": False,
        "skipped": True,
        "skip_reason": None,
        "dry_run": common.args.dry_run,
        "commit": None,
        "rescue_epoch": None,
        "pending_install_epoch": None,
        "drift_detected": False,
        "steps": steps,
        "error": None,
    }

    pending = get_pending_version(hostname, dev)
    if pending is None:
        logger.debug("check_and_reinstall: no pending version")
        result["skip_reason"] = "no_pending"
        return result

    commit_info = get_commit_information(dev)
    if commit_info is None:
        logger.debug("check_and_reinstall: cannot get commit information")
        result["skip_reason"] = "no_commit_info"
        return result

    commit_epoch, commit_dt_str, commit_user, commit_client = commit_info
    result["commit"] = {
        "epoch": commit_epoch,
        "datetime": commit_dt_str,
        "user": commit_user,
        "client": commit_client,
    }
    rescue_epoch = get_rescue_config_time(dev)
    result["rescue_epoch"] = rescue_epoch
    pending_install_epoch = get_pending_install_time(dev)
    result["pending_install_epoch"] = pending_install_epoch

    # Primary: does the pending image's install time cover the latest
    # commit? If so, the embedded config is current.
    if pending_install_epoch is not None:
        if commit_epoch <= pending_install_epoch:
            result["skip_reason"] = "pending_current"
            return result
        # Provable drift: commit happened after the pending install.
        result["drift_detected"] = True
        result["skip_reason"] = "drift_detected"
        steps.append({
            "action": "warning",
            "message": (
                f"\tWARNING: config modified "
                f"({commit_dt_str} by {commit_user} via {commit_client}) "
                f"after pending firmware was staged. Reboot will activate "
                f"firmware with the older embedded config. To refresh the "
                f"pending image, roll back the pending slot and re-run "
                f"``junos-ops upgrade``."
            ),
        })
        return result

    # Fallback when pending_install_epoch is not available.
    if rescue_epoch is not None:
        if commit_epoch <= rescue_epoch:
            result["skip_reason"] = "config_unchanged"
            return result
        # Can't be sure pending is stale (rescue_epoch ≠ install time),
        # but the commit postdates rescue, which is a soft signal.
        result["skip_reason"] = "cannot_verify"
        steps.append({
            "action": "warning",
            "message": (
                f"\tWARNING: config modified "
                f"({commit_dt_str} by {commit_user} via {commit_client}) "
                f"after last rescue save. Cannot determine whether the "
                f"pending image reflects this change; reboot will proceed."
            ),
        })
        return result

    # Neither marker available — emit a soft warning and skip.
    result["skip_reason"] = "cannot_verify"
    steps.append({
        "action": "warning",
        "message": (
            "\tWARNING: cannot determine pending install time and no "
            "rescue config exists. Unable to verify embedded config "
            "freshness; reboot will proceed."
        ),
    })
    return result


def reboot(hostname: str, dev, reboot_dt: datetime.datetime) -> dict:
    """Schedule device reboot at the specified time.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): True iff ``code == 0``.
        - ``code`` (int): legacy exit code (0 = success, 2 = cannot read
          reboot info, 3 = clear_reboot failed, 4 = ConnectError on
          reboot RPC, 5 = RpcError on reboot RPC, 6 =
          check_and_reinstall failed).
        - ``dry_run`` (bool)
        - ``reboot_at`` (str): formatted ``yymmddhhmm`` target time.
        - ``existing_schedule`` (str | None): summary of any
          pre-existing reboot/shutdown schedule detected.
        - ``cleared_existing`` (bool): True iff we forcibly cleared a
          pre-existing schedule.
        - ``reinstall_result`` (dict | None): nested
          :func:`check_and_reinstall` result.
        - ``message`` (str | None): PyEZ ``SW.reboot`` response (or
          dry-run equivalent).
        - ``steps`` (list[dict]): chronological progress for display.
        - ``error`` (str | None).

    Does not print. Note: the legacy implementation called
    ``dev.close()`` on success; callers should now close the device.
    """
    logger.debug(f"{reboot_dt=}")
    at_str = reboot_dt.strftime("%y%m%d%H%M")
    steps: list[dict] = []
    result: dict = {
        "hostname": hostname,
        "ok": False,
        "code": 0,
        "dry_run": common.args.dry_run,
        "reboot_at": at_str,
        "existing_schedule": None,
        "cleared_existing": False,
        "reinstall_result": None,
        "message": None,
        "steps": steps,
        "error": None,
    }

    xml_str: str = ""
    parse_error: Exception | None = None
    try:
        rpc = dev.rpc.get_reboot_information({"format": "text"})
        xml_str = etree.tostring(rpc, encoding="unicode")
    except ConnectError as err:
        logger.error(f"{err=}")
        result["code"] = 2
        result["error"] = "ConnectError"
        return result
    except etree.XMLSyntaxError as e:
        # PyEZ / lxml can fail to parse the ``get_reboot_information``
        # response on some devices that already have a halt schedule with
        # a custom message — observed on SRX345 running 22.4R3-S6.5 when
        # ``request system halt at "..." message "..."`` had been issued
        # earlier. The payload is downstream of NETCONF so we never see
        # the raw bytes here. Record the failure, surface a clear warning,
        # and fall back to the ``--force`` branch: the separate
        # ``clear_reboot`` RPC uses a different code path and works fine
        # against the same device. See issue #60.
        parse_error = e

    logger.debug(f"{xml_str=}")
    if parse_error is not None:
        logger.warning(
            f"{hostname}: get_reboot_information XML parse failed: {parse_error}"
        )
        steps.append({
            "action": "existing_schedule",
            "message": (
                "\tWARNING: cannot parse existing reboot schedule "
                f"({type(parse_error).__name__}: {parse_error}). "
                "Assume a schedule exists."
            ),
        })
        if not common.args.force:
            result["code"] = 3
            result["error"] = "get_reboot_information_parse_error"
            result["message"] = (
                "cannot parse existing reboot schedule; retry with --force "
                "to unconditionally clear the existing schedule before "
                "proceeding"
            )
            steps.append({
                "action": "error",
                "message": f"\t{result['message']}",
            })
            return result
        steps.append({
            "action": "force_clear",
            "message": "\tforce: clearing reboot schedule blindly (parse failed)",
        })
        clear_result = clear_reboot(dev)
        steps.append({"action": "clear_reboot", **clear_result})
        if not clear_result["ok"]:
            result["code"] = 3
            result["error"] = "clear_reboot_failed"
            return result
        result["cleared_existing"] = True
    elif xml_str.find("No shutdown/reboot scheduled.") < 0:
        logger.debug("ANY SHUTDWON/REBOOT SCHEDULE EXISTS")
        match = re.search(r"^(\w+) requested by (\w+) at (.*)$", xml_str, re.MULTILINE)
        if match and len(match.groups()) == 3:
            dt = datetime.datetime.strptime(match.group(3), "%a %b %d %H:%M:%S %Y")
            existing_msg = f"\t{match.group(1).upper()} SCHEDULE EXISTS AT {dt}"
            result["existing_schedule"] = existing_msg
            steps.append({"action": "existing_schedule", "message": existing_msg})
            if common.args.force:
                logger.debug("force clear reboot")
                steps.append({"action": "force_clear", "message": "\tforce: clear reboot"})
                clear_result = clear_reboot(dev)
                steps.append({"action": "clear_reboot", **clear_result})
                if not clear_result["ok"]:
                    result["code"] = 3
                    result["error"] = "clear_reboot_failed"
                    return result
                result["cleared_existing"] = True
            else:
                logger.debug("skip clear reboot")

    # config change detection + automatic re-install
    reinstall_result = check_and_reinstall(hostname, dev)
    result["reinstall_result"] = reinstall_result
    if not reinstall_result.get("ok"):
        result["code"] = 6
        result["error"] = "reinstall_failed"
        return result

    # reboot
    sw = SW(dev)
    try:
        if common.args.dry_run:
            msg = f"dry-run: reboot at {at_str}"
        else:
            msg = sw.reboot(at=at_str)
    except ConnectError as e:
        logger.error(f"{e=}")
        result["code"] = 4
        result["error"] = "ConnectError"
        return result
    except RpcError as e:
        logger.error(f"{e}")
        result["code"] = 5
        result["error"] = "RpcError"
        return result
    finally:
        del sw

    result["message"] = msg
    steps.append({"action": "reboot", "message": f"\t{msg}"})
    result["ok"] = True
    logger.debug("success")
    return result


def yymmddhhmm_type(dt_str: str) -> datetime.datetime:
    """Validate and parse YYMMDDHHMM datetime string for argparse."""
    try:
        return datetime.datetime.strptime(dt_str, "%y%m%d%H%M")
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"{e}: {dt_str} must be yymmddhhmm format. ex. 2501020304"
        )


def _run_health_check(hostname, dev, health_cmds) -> dict:
    """Run health check commands after commit confirmed.

    Try each command in order. Overall passes if any command succeeds.
    An "uptime" command string is treated as a special keyword and
    runs a NETCONF ``get-system-uptime-information`` RPC instead of
    issuing ``dev.cli("uptime")``.

    :param health_cmds: list of CLI commands or the ``uptime`` keyword
        to try.
    :return: dict with keys:

        - ``ok`` (bool): True iff at least one command passed.
        - ``passed_command`` (str | None): the command that succeeded,
          or None when every attempt failed.
        - ``commands`` (list[dict]): chronological per-command entries,
          each with ``command`` / ``kind`` (``"rpc"`` or ``"cli"``) /
          ``passed`` / ``message``.
        - ``steps`` (list[dict]): per-line step entries suitable for
          the display layer — each has ``action`` and ``message``,
          preserving the legacy ``\\thealth check: ...`` formatting
          line by line.
        - ``message`` (str): the concatenated multi-line text, equal
          to ``"\\n".join(step["message"] for step in steps)``.

    Does not print.
    """
    steps: list[dict] = []
    commands: list[dict] = []
    result = {
        "ok": False,
        "passed_command": None,
        "commands": commands,
        "steps": steps,
        "message": "",
    }

    def _step(action: str, msg: str) -> None:
        steps.append({"action": action, "message": msg})

    for health_cmd in health_cmds:
        entry: dict = {
            "command": health_cmd,
            "kind": None,
            "passed": False,
            "message": None,
        }

        # NETCONF RPC probe ("uptime" keyword).
        if health_cmd.strip() == "uptime":
            entry["kind"] = "rpc"
            _step("health_check", "\thealth check: uptime (NETCONF RPC)")
            try:
                reply = dev.rpc.get_system_uptime_information()
                current_time = reply.find(".//current-time/date-time")
                if current_time is not None and current_time.text:
                    entry["passed"] = True
                    entry["message"] = (
                        f"uptime: {current_time.text.strip()}"
                    )
                    _step(
                        "health_check_pass",
                        f"\thealth check passed "
                        f"(uptime: {current_time.text.strip()})",
                    )
                    commands.append(entry)
                    result["ok"] = True
                    result["passed_command"] = health_cmd
                    result["message"] = "\n".join(s["message"] for s in steps)
                    return result
                else:
                    entry["message"] = "no valid uptime data"
                    _step("health_check_warn", "\thealth check: no valid uptime data")
                    commands.append(entry)
                    continue
            except Exception as e:
                entry["message"] = f"RPC error: {e}"
                logger.debug(f"{hostname}: health check RPC failed: {e}")
                _step("health_check_error", f"\thealth check error: {e}")
                commands.append(entry)
                continue

        # CLI command
        entry["kind"] = "cli"
        _step("health_check", f"\thealth check: {health_cmd}")
        try:
            output = dev.cli(health_cmd)
        except Exception as e:
            entry["message"] = f"CLI error: {e}"
            logger.debug(f"{hostname}: health check command failed: {e}")
            _step("health_check_error", f"\thealth check error: {e}")
            commands.append(entry)
            continue

        if health_cmd.strip().startswith("ping"):
            match = re.search(r"(\d+) packets received", output)
            if match and int(match.group(1)) > 0:
                entry["passed"] = True
                entry["message"] = f"{match.group(1)} packets received"
                _step(
                    "health_check_pass",
                    f"\thealth check passed "
                    f"({match.group(1)} packets received)",
                )
                commands.append(entry)
                result["ok"] = True
                result["passed_command"] = health_cmd
                result["message"] = "\n".join(s["message"] for s in steps)
                return result
            else:
                entry["message"] = "no packets received"
                _step("health_check_warn", "\thealth check: no packets received")
                commands.append(entry)
                continue
        else:
            # Non-ping CLI: success if no exception raised.
            entry["passed"] = True
            entry["message"] = "ok"
            _step("health_check_pass", "\thealth check passed")
            commands.append(entry)
            result["ok"] = True
            result["passed_command"] = health_cmd
            result["message"] = "\n".join(s["message"] for s in steps)
            return result

    # Every attempted command failed.
    result["message"] = "\n".join(s["message"] for s in steps)
    return result


def load_config(hostname, dev, configfile) -> dict:
    """Load set command file and commit to device.

    Commit flow (default):
        lock -> load -> diff -> commit_check -> commit confirmed ->
        health check -> confirm -> unlock.
    Commit flow (``--no-confirm``):
        lock -> load -> diff -> commit_check -> commit -> unlock.
    On error, rollback + unlock for cleanup.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): overall success.
        - ``dry_run`` (bool)
        - ``configfile`` (str): the input file path.
        - ``template`` (bool): True iff the input is a ``.j2`` Jinja2
          template (rendered through ``common.render_template``).
        - ``rendered_commands`` (list[str] | None): the set commands
          actually loaded (template output or file lines).
        - ``diff`` (str | None): the device-reported diff; None means
          "no changes".
        - ``no_changes`` (bool): True iff ``diff is None`` — shortcut
          for the common happy path.
        - ``commit_mode`` (str): ``"confirmed"`` | ``"no_confirm"`` |
          ``"dry_run"`` | ``"none"`` (no commit happened, e.g. lock
          failed or no_changes).
        - ``confirm_timeout`` (int | None): confirm timeout in minutes
          for ``commit_mode == "confirmed"``, else None.
        - ``health_check`` (dict): health-check outcome, with keys:

          * ``ran`` (bool): True iff the health-check phase executed
            (``--no-health-check`` skips it).
          * ``commands_tried`` (list[str]): commands in the order they
            were attempted (the ``uptime`` keyword counts as one).
          * ``passed`` (bool): True iff at least one command passed
            and the final commit confirm was issued.
          * ``passed_command`` (str | None): the command that passed,
            or None if none did / the phase was skipped.
          * ``commands`` (list[dict]): per-command detail from
            :func:`_run_health_check` — each entry has ``command`` /
            ``kind`` (``"rpc"`` or ``"cli"``) / ``passed`` / ``message``.
        - ``steps`` (list[dict]): chronological progress entries, each
          with an ``action`` key and a ``message`` for display.
        - ``error`` (str | None): short identifier of the first fatal
          failure.
        - ``error_message`` (str | None): full error detail.

    During execution, each step is also echoed via ``logger.debug`` so
    operators watching the log see real-time progress. Does not print.
    """
    steps: list[dict] = []
    result: dict = {
        "hostname": hostname,
        "ok": False,
        "dry_run": common.args.dry_run,
        "configfile": configfile,
        "template": configfile.endswith(".j2"),
        "rendered_commands": None,
        "diff": None,
        "no_changes": False,
        "commit_mode": "none",
        "confirm_timeout": None,
        "health_check": {"ran": False, "commands_tried": [], "passed": False},
        "steps": steps,
        "error": None,
        "error_message": None,
    }

    def _step(action: str, message: str, **extra) -> None:
        """Append to steps and echo via logger.debug for live progress."""
        entry = {"action": action, "message": message}
        entry.update(extra)
        steps.append(entry)
        logger.debug(f"{hostname}: {message.strip()}")

    cu = Config(dev)

    # acquire config lock
    try:
        cu.lock()
    except Exception as e:
        logger.debug(f"{hostname}: config lock failed: {e}")
        result["error"] = "lock_failed"
        result["error_message"] = str(e)
        _step("lock", f"\tconfig lock failed: {e}", ok=False)
        return result

    try:
        # Load set command file (strip comments/blank lines)
        if result["template"]:
            commands = common.render_template(configfile, hostname, dev)
            _step("template", f"\ttemplate rendered: {len(commands)} command(s)")
        else:
            commands = common.load_commands(configfile)
        result["rendered_commands"] = commands
        cu.load("\n".join(commands), format="set")

        # diff
        diff = cu.diff()
        result["diff"] = diff
        if diff is None:
            result["no_changes"] = True
            result["ok"] = True
            _step("diff", "\tno changes")
            cu.unlock()
            return result

        # Template mode: echo rendered commands
        if result["template"]:
            for cmd in commands:
                _step("rendered_command", f"\t  {cmd}")

        # dry-run: diff only, no commit
        if common.args.dry_run:
            _step("dry_run", "\tdry-run: rollback (no commit)")
            cu.rollback()
            cu.unlock()
            result["commit_mode"] = "dry_run"
            result["ok"] = True
            return result

        # validation
        cu.commit_check()
        _step("commit_check", "\tcommit check passed")

        no_confirm = getattr(common.args, "no_confirm", False)
        if no_confirm:
            # Direct commit (skip commit confirmed)
            cu.commit()
            result["commit_mode"] = "no_confirm"
            _step("commit", "\tcommit applied (no confirm)")
        else:
            # commit confirmed (auto-rollback on timer)
            confirm_timeout = getattr(common.args, "confirm_timeout", 1)
            result["confirm_timeout"] = confirm_timeout
            result["commit_mode"] = "confirmed"
            cu.commit(confirm=confirm_timeout)
            _step("commit_confirmed",
                  f"\tcommit confirmed {confirm_timeout} applied")

            # health check
            no_health_check = getattr(common.args, "no_health_check", False)
            health_cmds = getattr(common.args, "health_check", None)
            if not no_health_check:
                if health_cmds is None:
                    health_cmds = ["uptime"]
                result["health_check"]["ran"] = True
                result["health_check"]["commands_tried"] = list(health_cmds)
                hc_result = _run_health_check(hostname, dev, health_cmds)
                # Propagate each health check step into load_config's
                # own step list (and logger.info) so the display layer
                # sees the same per-line feedback as pre-0.14.1.
                for hc_step in hc_result["steps"]:
                    _step(hc_step["action"], hc_step["message"])
                result["health_check"]["passed_command"] = hc_result.get("passed_command")
                result["health_check"]["commands"] = hc_result.get("commands", [])
                if not hc_result["ok"]:
                    # Failed — do not confirm; timer will auto-rollback.
                    _step(
                        "health_check_failed",
                        f"\thealth check FAILED — config will auto-rollback "
                        f"in {confirm_timeout} minute(s)",
                    )
                    logger.debug(
                        f"{hostname}: health check failed, "
                        f"not confirming commit"
                    )
                    cu.unlock()
                    result["health_check"]["passed"] = False
                    result["error"] = "health_check_failed"
                    return result
                result["health_check"]["passed"] = True

            # confirm (cancel timer)
            cu.commit()
            _step("commit_confirm_final",
                  "\tcommit confirmed, changes are now permanent")

        result["ok"] = True

    except Exception as e:
        logger.debug(f"{hostname}: config push failed: {e}")
        _step("exception", f"\tconfig push failed: {e}", ok=False)
        result["error"] = "exception"
        result["error_message"] = str(e)
        try:
            cu.rollback()
        except Exception:
            pass
        try:
            cu.unlock()
        except Exception:
            pass
        return result

    cu.unlock()
    return result
