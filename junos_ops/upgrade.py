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
import os
import re
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
        rpc = dev.rpc.request_snapshot(delete="*", dev_timeout=60)
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

    # pre-flight skip checks
    if common.args.force:
        logger.debug("copy: force copy")
    else:
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
        if check_remote_package(hostname, dev):
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "already_copied"
            steps.append({
                "action": "skip",
                "ok": True,
                "message": "remote package is already copied successfully",
            })
            return result

    # request-system-storage-cleanup
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

    # EX/QFX: snapshot delete to free disk
    snap = delete_snapshots(dev)
    result["snapshot_delete"] = snap
    if snap.get("applied"):
        steps.append({"action": "snapshot_delete", **snap})

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


def clear_reboot(dev) -> bool:
    """Clear scheduled reboot."""
    if common.args.dry_run:
        print("\tdry-run: clear system reboot")
    else:
        try:
            rpc = dev.rpc.clear_reboot({"format": "text"})
            xml_str = etree.tostring(rpc, encoding="unicode")
            logger.debug(f"{rpc=} {xml_str=}")
            if (
                xml_str.find("No shutdown/reboot scheduled.") >= 0
                or xml_str.find("Terminating...") >= 0
            ):
                logger.debug("clear reboot schedule successful")
                print("\tclear reboot schedule successful")
            else:
                logger.debug("clear reboot schedule failed")
                print("\tclear reboot schedule failed")
                return True
        except RpcError as e:
            logger.error(f"Clear reboot failure caused by RpcError: {e}")
            return True
        except RpcTimeoutError as e:
            logger.error(f"Clear reboot failure caused by RpcTimeoutError: {e}")
            return True
        except Exception as e:
            logger.error(e)
            return True
    return False


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
    if common.args.dry_run and (common.args.copy or common.args.update):
        steps.append({
            "action": "remote_check",
            "message": "dry-run: skip remote package check",
        })
    elif check_remote_package(hostname, dev) is not True and common.args.install:
        logger.info(
            "remote package file not found. Please consider --copy before --install"
        )
        result["skip_reason"] = "remote_missing"
        result["error"] = "remote_missing"
        steps.append({
            "action": "remote_check",
            "message": "remote package file not found. "
                       "Please consider --copy before --install",
        })
        return result

    copy_result = copy(hostname, dev)
    result["copy_result"] = copy_result
    if not copy_result.get("ok"):
        result["error"] = "copy_failed"
        return result

    if clear_reboot(dev):
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
        logger.info("install successful")
        result["ok"] = True
        steps.append({"action": "sw_install", "ok": True, "message": msg})
    else:
        logger.info("install failed")
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


def check_local_package(hostname, dev):
    """Check local package checksum.

    :returns:
       * ``True`` file found, checksum correct.
       * ``False`` file found, checksum incorrect.
       * ``None`` file not found.
    """
    # local package check
    # model, file, hash, algo
    model = dev.facts["model"]
    file = get_model_file(hostname, model)
    local_file = get_local_path(hostname, file)
    pkg_hash = get_model_hash(hostname, model)
    if len(file) == 0 or len(pkg_hash) == 0:
        return None
    algo = common.config.get(hostname, "hashalgo")
    sw = SW(dev)
    if get_hashcache("localhost", file) == pkg_hash:
        print(f"  - local package: {local_file} is found. checksum(cache) is OK.")
        return True
    ret = None
    try:
        val = sw.local_checksum(local_file, algorithm=algo)
        if val == pkg_hash:
            print(f"  - local package: {local_file} is found. checksum is OK.")
            set_hashcache("localhost", file, val)
            ret = True
        else:
            print(f"  - local package: {local_file} is found. checksum is BAD. COPY AGAIN!")
            ret = False
    except FileNotFoundError as e:
        print(f"  - local package: {local_file} is not found.")
        logger.debug(e)
    except Exception as e:
        logger.error(e)
    del sw
    return ret


def check_remote_package(hostname, dev):
    """Check remote package checksum.

    :returns:
       * ``True`` file found, checksum correct.
       * ``False`` file found, checksum incorrect.
       * ``None`` file not found.
    """
    # remote package check
    # model, file, hash, algo
    model = dev.facts["model"]
    file = get_model_file(hostname, model)
    pkg_hash = get_model_hash(hostname, model)
    if len(file) == 0 or len(pkg_hash) == 0:
        return None
    algo = common.config.get(hostname, "hashalgo")
    sw = SW(dev)
    ret = None
    if get_hashcache(hostname, file) == pkg_hash:
        print(f"  - remote package: {file} is found. checksum(cache) is OK.")
        return True
    try:
        val = sw.remote_checksum(
            common.config.get(hostname, "rpath") + "/" + file, algorithm=algo
        )
        if val is None:
            print(f"  - remote package: {file} is not found.")
        elif val == pkg_hash:
            print(f"  - remote package: {file} is found. checksum is OK.")
            set_hashcache(hostname, file, val)
            ret = True
        else:
            print(f"  - remote package: {file} is found. checksum is BAD. COPY AGAIN!")
            ret = False
    except RpcError as e:
        logger.error("Unable to remote checksum: {0}".format(e))
    except Exception as e:
        logger.error(e)
    del sw
    return ret


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
        - ``local_package`` (bool | None): result of
          :func:`check_local_package` (True=ok, False=bad, None=missing).
        - ``remote_package`` (bool | None): result of
          :func:`check_remote_package`.
        - ``ok`` (bool): True iff both ``local_package`` and
          ``remote_package`` are True.

    Note: the nested ``check_local_package`` / ``check_remote_package``
    helpers still print to stdout in the current refactor step.
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
        "ok": bool(local and remote),
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
            logger.debug(f"get_snapshot_information: xml={etree.dump(xml)}")
            primary = False
            for i in range(len(xml)):
                logger.debug(
                    f"get_snapshot_information: i={i}, tag={xml[i].tag}, text={xml[i].text}"
                )
                if (
                    xml[i].tag == "snapshot-medium"
                    and re.match(".*primary", xml[i].text, re.MULTILINE | re.DOTALL)
                    is not None
                ):
                    logger.debug("primary find")
                    primary = True
                if (
                    primary
                    and xml[i].tag == "software-version"
                    and xml[i][0].tag == "package"
                    and xml[i][0][1].tag == "package-version"
                ):
                    pending = xml[i][0][1].text.strip()
                    break
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
                print(e)
                return None
        else:
            print("Unknown personality:", dev.facts)
            return None
    except RpcError as e:
        print("Show version failure caused by RpcError:", e)
        return None
    except RpcTimeoutError as e:
        print("Show version failure caused by RpcTimeoutError:", e)
        return None
    except Exception as e:
        print(e)
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
    """Re-install firmware if config was modified after the last install.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``ok`` (bool): True on success (including the common "no
          action needed" path); False only if rescue save or re-install
          raised an error.
        - ``reinstalled`` (bool): True iff the PyEZ install RPC was
          actually invoked (not dry-run, not skipped).
        - ``skipped`` (bool): True when we decided no re-install was
          needed.
        - ``skip_reason`` (str | None): ``"no_pending"`` |
          ``"no_commit_info"`` | ``"config_unchanged"`` | ``"dry_run"``
          | None.
        - ``dry_run`` (bool)
        - ``commit`` (dict | None): ``{epoch, datetime, user, client}``
          of the most recent commit, when available.
        - ``rescue_epoch`` (int | None): mtime of the rescue config.
        - ``rescue_save`` (dict | None): ``{ok, message, error}``.
        - ``install_message`` (str | None): PyEZ ``SW.install`` message.
        - ``steps`` (list[dict]): chronological progress for display.
        - ``error`` (str | None).

    Does not print.
    """
    steps: list[dict] = []
    result: dict = {
        "hostname": hostname,
        "ok": True,
        "reinstalled": False,
        "skipped": False,
        "skip_reason": None,
        "dry_run": common.args.dry_run,
        "commit": None,
        "rescue_epoch": None,
        "rescue_save": None,
        "install_message": None,
        "steps": steps,
        "error": None,
    }

    pending = get_pending_version(hostname, dev)
    if pending is None:
        logger.debug("check_and_reinstall: no pending version, skip")
        result["skipped"] = True
        result["skip_reason"] = "no_pending"
        return result

    commit_info = get_commit_information(dev)
    if commit_info is None:
        logger.debug("check_and_reinstall: cannot get commit information, skip")
        result["skipped"] = True
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

    if rescue_epoch is not None and commit_epoch <= rescue_epoch:
        logger.debug("check_and_reinstall: config not modified after rescue save, skip")
        result["skipped"] = True
        result["skip_reason"] = "config_unchanged"
        return result

    # Config modified after rescue save (or no rescue config exists).
    if rescue_epoch is None:
        steps.append({
            "action": "warning",
            "message": "\tWARNING: rescue config not found. "
                       "Re-installing firmware with current config.",
        })
    else:
        steps.append({
            "action": "warning",
            "message": (
                f"\tWARNING: config modified after firmware install "
                f"({commit_dt_str} by {commit_user} via {commit_client})."
            ),
        })
        steps.append({
            "action": "warning",
            "message": "\tRe-installing firmware to validate current config.",
        })

    if common.args.dry_run:
        result["skipped"] = True
        result["skip_reason"] = "dry_run"
        steps.append({
            "action": "dry_run",
            "message": "\tdry-run: re-install and rescue config save skipped",
        })
        return result

    # rescue config save
    rescue_save: dict = {"ok": False, "message": None, "error": None}
    cu = Config(dev)
    try:
        saved = cu.rescue("save")
        if saved:
            rescue_save["ok"] = True
            rescue_save["message"] = "\tre-install: rescue config save successful"
        else:
            rescue_save["message"] = "\tre-install: rescue config save failed"
    except Exception as e:
        rescue_save["error"] = type(e).__name__
        rescue_save["message"] = f"\tre-install: rescue config save failed: {e}"
        logger.error(f"check_and_reinstall: rescue save failed: {e}")
    result["rescue_save"] = rescue_save
    steps.append({"action": "rescue_save", **rescue_save})
    if not rescue_save["ok"]:
        result["ok"] = False
        result["error"] = "rescue_save_failed"
        return result

    # re-install (with validation)
    try:
        sw = SW(dev)
        try:
            status, msg = sw.install(
                get_model_file(hostname, dev.facts["model"]),
                remote_path=common.config.get(hostname, "rpath"),
                progress=True,
                validate=True,
                cleanfs=False,
                no_copy=True,
                issu=False,
                nssu=False,
                timeout=2400,
                checksum=get_model_hash(hostname, dev.facts["model"]),
                checksum_timeout=1200,
                checksum_algorithm=common.config.get(hostname, "hashalgo"),
                all_re=True,
            )
        finally:
            del sw
        logger.debug(f"check_and_reinstall: {msg=}")
        result["install_message"] = msg
        result["reinstalled"] = True
        if status:
            steps.append({
                "action": "reinstall",
                "ok": True,
                "message": "\tre-install: successful",
            })
        else:
            result["ok"] = False
            result["error"] = "reinstall_failed"
            steps.append({
                "action": "reinstall",
                "ok": False,
                "message": f"\tre-install: failed: {msg}",
            })
    except Exception as e:
        result["ok"] = False
        result["error"] = type(e).__name__
        logger.error(f"check_and_reinstall: install failed: {e}")
        steps.append({
            "action": "reinstall",
            "ok": False,
            "message": f"\tre-install: failed: {e}",
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

    try:
        rpc = dev.rpc.get_reboot_information({"format": "text"})
    except ConnectError as err:
        logger.error(f"{err=}")
        result["code"] = 2
        result["error"] = "ConnectError"
        return result
    xml_str = etree.tostring(rpc, encoding="unicode")
    logger.debug(f"{xml_str=}")
    if xml_str.find("No shutdown/reboot scheduled.") < 0:
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
                if clear_reboot(dev):
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


def _run_health_check(hostname, dev, health_cmds) -> bool:
    """Run health check commands after commit confirmed.

    Try each command in order. Pass if any succeeds.

    :param health_cmds: list of CLI commands or "rpc" keyword to try.
    :return: True on failure (all commands failed), False on success.
    """
    for health_cmd in health_cmds:
        # NETCONF RPC probe
        if health_cmd.strip() == "uptime":
            print("\thealth check: uptime (NETCONF RPC)")
            try:
                reply = dev.rpc.get_system_uptime_information()
                current_time = reply.find(".//current-time/date-time")
                if current_time is not None and current_time.text:
                    print(f"\thealth check passed "
                          f"(uptime: {current_time.text.strip()})")
                    return False
                else:
                    print("\thealth check: no valid uptime data")
                    continue
            except Exception as e:
                logger.error(f"{hostname}: health check RPC failed: {e}")
                print(f"\thealth check error: {e}")
                continue

        print(f"\thealth check: {health_cmd}")
        try:
            output = dev.cli(health_cmd)
        except Exception as e:
            logger.error(f"{hostname}: health check command failed: {e}")
            print(f"\thealth check error: {e}")
            continue

        # ping コマンドの場合: "N packets received" を解析
        if health_cmd.strip().startswith("ping"):
            match = re.search(r"(\d+) packets received", output)
            if match and int(match.group(1)) > 0:
                print(f"\thealth check passed "
                      f"({match.group(1)} packets received)")
                return False
            else:
                print(f"\thealth check: no packets received")
                continue
        else:
            # ping 以外: 例外なく実行できれば成功
            print(f"\thealth check passed")
            return False

    # 全コマンド失敗
    return True


def load_config(hostname, dev, configfile) -> bool:
    """Load set command file and commit to device.

    Commit flow (default):
        lock -> load -> diff -> commit_check -> commit confirmed -> health check -> confirm -> unlock.
    Commit flow (--no-confirm):
        lock -> load -> diff -> commit_check -> commit -> unlock.
    On error, rollback + unlock for cleanup.

    :return: True on error, False on success.
    """
    cu = Config(dev)

    # config ロック取得
    try:
        cu.lock()
    except Exception as e:
        logger.error(f"{hostname}: config lock failed: {e}")
        print(f"\tconfig lock failed: {e}")
        return True

    try:
        # set コマンドファイル読み込み（コメント行・空行を除去）
        if configfile.endswith(".j2"):
            commands = common.render_template(configfile, hostname, dev)
            print(f"\ttemplate rendered: {len(commands)} command(s)")
        else:
            commands = common.load_commands(configfile)
        cu.load("\n".join(commands), format="set")

        # 差分確認
        diff = cu.diff()
        if diff is None:
            print("\tno changes")
            cu.unlock()
            return False

        # テンプレート使用時はレンダリング結果を表示
        if configfile.endswith(".j2"):
            for cmd in commands:
                print(f"\t  {cmd}")

        cu.pdiff()

        # dry-run: diff 表示のみで終了
        if common.args.dry_run:
            print("\tdry-run: rollback (no commit)")
            cu.rollback()
            cu.unlock()
            return False

        # validation
        cu.commit_check()
        print("\tcommit check passed")

        no_confirm = getattr(common.args, "no_confirm", False)
        if no_confirm:
            # 直接 commit（commit confirmed をスキップ）
            cu.commit()
            print("\tcommit applied (no confirm)")
        else:
            # commit confirmed（自動ロールバック付き）
            confirm_timeout = getattr(common.args, "confirm_timeout", 1)
            cu.commit(confirm=confirm_timeout)
            print(f"\tcommit confirmed {confirm_timeout} applied")

            # ヘルスチェック
            no_health_check = getattr(common.args, "no_health_check", False)
            health_cmds = getattr(common.args, "health_check", None)
            if not no_health_check:
                if health_cmds is None:
                    health_cmds = ["ping count 3 255.255.255.255 rapid"]
                if _run_health_check(hostname, dev, health_cmds):
                    # 失敗 — 最終 commit を送らず、タイマー満了で自動ロールバック
                    print(f"\thealth check FAILED — config will auto-rollback "
                          f"in {confirm_timeout} minute(s)")
                    logger.error(f"{hostname}: health check failed, "
                                 f"not confirming commit")
                    cu.unlock()
                    return True

            # 確定（タイマー解除）
            cu.commit()
            print("\tcommit confirmed, changes are now permanent")

    except Exception as e:
        logger.error(f"{hostname}: config push failed: {e}")
        print(f"\tconfig push failed: {e}")
        try:
            cu.rollback()
        except Exception:
            pass
        try:
            cu.unlock()
        except Exception:
            pass
        return True

    cu.unlock()
    return False
