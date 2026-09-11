"""Tests for ``upgrade.copy``: pre-flight skips, storage cleanup gating,
remote-package check, and every SCP outcome.

The ordering inside ``copy()`` is load-bearing (see the comments in
upgrade.py around the storage cleanup): cleanup must run *before* the
remote check because it sweeps /var/tmp where the staged package lives,
and a failed cleanup must stop the whole operation. Nothing enforced
that before these tests.
"""

from unittest.mock import MagicMock, patch

import pytest
from jnpr.junos.exception import RpcTimeoutError
from lxml import etree
from ncclient.operations.errors import TimeoutExpiredError

from junos_ops import upgrade

HOST = "test-host"
MODEL = "ex2300-24t"
FILE = "junos-arm-32-22.4R3-S6.5.tgz"


def _dev(personality="SWITCH", cleanup_xml="<success/>"):
    dev = MagicMock()
    dev.facts = {"model": MODEL, "personality": personality}
    dev.rpc.request_system_storage_cleanup.return_value = etree.fromstring(
        f"<output>{cleanup_xml}</output>"
    )
    return dev


def _steps(result, action):
    return [s for s in result["steps"] if s["action"] == action]


@pytest.fixture
def copy_env(mock_args, mock_config):
    """Patch every collaborator of copy() and hand back the mocks.

    Defaults describe the "fresh device, nothing staged yet" case so each
    test only overrides what it is about.
    """
    with (
        patch.object(upgrade, "check_running_package", return_value={"match": False}) as running,
        patch.object(
            upgrade, "check_remote_package",
            return_value={"status": "missing", "file": FILE, "message": "not found"},
        ) as remote,
        patch.object(
            upgrade, "delete_snapshots",
            return_value={"applied": False, "ok": True, "dry_run": False,
                          "message": None, "error": None},
        ) as snap,
        patch.object(upgrade, "clear_hashcache") as clear,
        patch.object(upgrade, "SW") as sw_cls,
    ):
        sw_cls.return_value.safe_copy.return_value = True
        yield {
            "running": running,
            "remote": remote,
            "snap": snap,
            "clear": clear,
            "SW": sw_cls,
            "args": mock_args,
        }


class TestPreflightSkips:
    def test_already_running_skips_everything(self, copy_env):
        copy_env["running"].return_value = {"match": True}
        dev = _dev()
        result = upgrade.copy(HOST, dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "already_running"
        dev.rpc.request_system_storage_cleanup.assert_not_called()
        copy_env["SW"].assert_not_called()

    def test_force_bypasses_running_and_remote_checks(self, copy_env):
        copy_env["args"].force = True
        copy_env["running"].return_value = {"match": True}
        result = upgrade.copy(HOST, _dev())
        copy_env["running"].assert_not_called()
        copy_env["remote"].assert_not_called()
        assert result["ok"] is True
        assert result["skipped"] is False
        # force is forwarded to safe_copy so a same-checksum file is re-sent
        assert copy_env["SW"].return_value.safe_copy.call_args.kwargs["force_copy"] is True


class TestStorageCleanupGate:
    def test_cleanup_failure_stops_before_snapshot_and_remote_check(self, copy_env):
        dev = _dev(cleanup_xml="<failure/>")
        result = upgrade.copy(HOST, dev)
        assert result["ok"] is False
        assert result["error"] == "storage_cleanup_failed"
        assert result["storage_cleanup"]["ok"] is False
        assert result["snapshot_delete"] is None
        copy_env["snap"].assert_not_called()
        copy_env["remote"].assert_not_called()
        copy_env["clear"].assert_not_called()
        copy_env["SW"].assert_not_called()

    def test_cleanup_exception_is_captured(self, copy_env):
        dev = _dev()
        dev.rpc.request_system_storage_cleanup.side_effect = RpcTimeoutError(dev, "cleanup", 60)
        result = upgrade.copy(HOST, dev)
        assert result["error"] == "storage_cleanup_failed"
        assert result["storage_cleanup"]["error"] == "RpcTimeoutError"
        assert "RpcTimeoutError" in result["storage_cleanup"]["message"]
        copy_env["SW"].assert_not_called()

    def test_cleanup_runs_before_remote_check_and_clears_hashcache(self, copy_env):
        """Cleanup sweeps /var/tmp: the cached checksum must be dropped and
        the remote check must happen only after the cleanup."""
        order = []
        dev = _dev()
        dev.rpc.request_system_storage_cleanup.side_effect = lambda **kw: (
            order.append("cleanup"), etree.fromstring("<output><success/></output>")
        )[1]
        copy_env["remote"].side_effect = lambda h, d: (
            order.append("remote"),
            {"status": "missing", "file": FILE, "message": "not found"},
        )[1]
        upgrade.copy(HOST, dev)
        assert order == ["cleanup", "remote"]
        copy_env["clear"].assert_called_once_with(HOST, FILE)

    def test_dry_run_skips_cleanup_rpc_and_hashcache(self, copy_env):
        copy_env["args"].dry_run = True
        dev = _dev()
        result = upgrade.copy(HOST, dev)
        dev.rpc.request_system_storage_cleanup.assert_not_called()
        copy_env["clear"].assert_not_called()
        assert result["storage_cleanup"]["dry_run"] is True
        assert result["storage_cleanup"]["ok"] is True


class TestSnapshotDelete:
    def test_not_applied_is_not_a_step(self, copy_env):
        result = upgrade.copy(HOST, _dev(personality="MX"))
        assert _steps(result, "snapshot_delete") == []
        assert result["snapshot_delete"]["applied"] is False

    def test_applied_failure_is_non_fatal(self, copy_env):
        copy_env["snap"].return_value = {
            "applied": True, "ok": True, "dry_run": False,
            "message": "snapshot delete failed: RpcError", "error": "RpcError",
        }
        result = upgrade.copy(HOST, _dev())
        assert len(_steps(result, "snapshot_delete")) == 1
        assert result["ok"] is True  # scp still ran and succeeded
        copy_env["SW"].return_value.safe_copy.assert_called_once()


class TestRemoteCheck:
    def test_already_copied_skips_scp(self, copy_env):
        copy_env["remote"].return_value = {"status": "ok", "file": FILE, "message": "OK"}
        result = upgrade.copy(HOST, _dev())
        assert result["ok"] is True
        assert result["skip_reason"] == "already_copied"
        copy_env["SW"].assert_not_called()
        assert _steps(result, "remote_check")[0]["ok"] is True

    @pytest.mark.parametrize(
        "status, phrase",
        [("bad", "checksum mismatch; overwriting"), ("missing", "not present; copying")],
    )
    def test_bad_or_missing_rewrites_message_and_copies(self, copy_env, status, phrase):
        copy_env["remote"].return_value = {
            "status": status, "file": FILE, "message": "BAD. COPY AGAIN!",
        }
        result = upgrade.copy(HOST, _dev())
        step = _steps(result, "remote_check")[0]
        assert step["ok"] is False
        assert phrase in step["message"]
        assert "COPY AGAIN" not in step["message"]
        copy_env["SW"].return_value.safe_copy.assert_called_once()
        assert result["ok"] is True


class TestScp:
    def test_dry_run_reports_without_scp(self, copy_env):
        copy_env["args"].dry_run = True
        result = upgrade.copy(HOST, _dev())
        step = _steps(result, "scp")[0]
        assert step["dry_run"] is True
        assert FILE in step["message"]
        assert result["local_file"].endswith(FILE)
        assert result["ok"] is True
        copy_env["SW"].assert_not_called()

    def test_success_passes_checksum_and_paths(self, copy_env):
        result = upgrade.copy(HOST, _dev())
        kw = copy_env["SW"].return_value.safe_copy.call_args.kwargs
        assert kw["remote_path"] == "/var/tmp"
        assert kw["checksum"] == "abc123def456"
        assert kw["checksum_algorithm"] == "md5"
        assert kw["force_copy"] is False
        assert result["ok"] is True
        assert result["error"] is None
        assert _steps(result, "scp")[0]["ok"] is True

    def test_safe_copy_false_is_scp_failed(self, copy_env):
        copy_env["SW"].return_value.safe_copy.return_value = False
        result = upgrade.copy(HOST, _dev())
        assert result["ok"] is False
        assert result["error"] == "scp_failed"
        assert _steps(result, "scp")[0]["error"] == "scp_failed"

    @pytest.mark.parametrize(
        "exc, name",
        [
            (TimeoutExpiredError("ncclient timeout"), "TimeoutExpiredError"),
            (RpcTimeoutError(MagicMock(), "scp", 1200), "RpcTimeoutError"),
            (OSError("No such file"), "OSError"),
        ],
    )
    def test_exceptions_map_to_error_names(self, copy_env, exc, name):
        copy_env["SW"].return_value.safe_copy.side_effect = exc
        result = upgrade.copy(HOST, _dev())
        assert result["ok"] is False
        assert result["error"] == name
        step = _steps(result, "scp")[0]
        assert step["ok"] is False
        assert step["error"] == name
