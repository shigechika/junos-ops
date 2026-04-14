"""Tests for the package-check helpers migrated to dict returns in v0.14.1.

``check_local_package``, ``check_remote_package``, and ``clear_reboot``
previously printed directly to stdout (``- local package: FILE is
found. checksum is OK.`` etc.) and returned bools. As of junos-ops
0.14.1 they return structured dicts with a ``status`` field (or
``ok`` for clear_reboot) and no longer print — that is the last
blocker to eliminating ``contextlib.redirect_stdout`` from junos-mcp.

These tests lock in both behaviours:

1. The dict schema for each ``status`` branch.
2. ``capsys`` assertion that the core helper emitted nothing on
   stdout.
"""

from unittest.mock import MagicMock, patch

from jnpr.junos.exception import RpcError


# -------------------------------------------------------------------
# check_local_package
# -------------------------------------------------------------------


class TestCheckLocalPackage:
    def _dev(self):
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        return dev

    def test_cache_hit(self, junos_upgrade, mock_args, mock_config, capsys):
        """Hash cache hit: status=ok, cached=True, no SW.local_checksum call."""
        junos_upgrade.set_hashcache("localhost", "junos-arm-32-22.4R3-S6.5.tgz", "abc123def456")
        with patch.object(junos_upgrade, "SW") as MockSW:
            result = junos_upgrade.check_local_package("test-host", self._dev())
        assert result["status"] == "ok"
        assert result["cached"] is True
        assert result["actual_hash"] == "abc123def456"
        assert "checksum(cache) is OK" in result["message"]
        assert result["error"] is None
        MockSW.return_value.local_checksum.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_checksum_ok(self, junos_upgrade, mock_args, mock_config, capsys):
        """Fresh checksum matches: status=ok, cached=False."""
        with patch.object(
            junos_upgrade, "_compute_local_checksum", return_value="abc123def456"
        ) as mock_chk:
            result = junos_upgrade.check_local_package("test-host", self._dev())
        assert result["status"] == "ok"
        assert result["cached"] is False
        assert result["actual_hash"] == "abc123def456"
        assert "checksum is OK" in result["message"]
        mock_chk.assert_called_once()
        assert capsys.readouterr().out == ""

    def test_checksum_bad(self, junos_upgrade, mock_args, mock_config, capsys):
        """Fresh checksum mismatches: status=bad."""
        with patch.object(
            junos_upgrade, "_compute_local_checksum", return_value="WRONG_HASH"
        ):
            result = junos_upgrade.check_local_package("test-host", self._dev())
        assert result["status"] == "bad"
        assert result["actual_hash"] == "WRONG_HASH"
        assert result["expected_hash"] == "abc123def456"
        assert "BAD" in result["message"]
        assert capsys.readouterr().out == ""

    def test_file_missing(self, junos_upgrade, mock_args, mock_config, capsys):
        """FileNotFoundError: status=missing, actual_hash=None."""
        with patch.object(
            junos_upgrade,
            "_compute_local_checksum",
            side_effect=FileNotFoundError("no such file"),
        ):
            result = junos_upgrade.check_local_package("test-host", self._dev())
        assert result["status"] == "missing"
        assert result["actual_hash"] is None
        assert "is not found" in result["message"]
        assert capsys.readouterr().out == ""

    def test_generic_error(self, junos_upgrade, mock_args, mock_config, capsys):
        """Unexpected exception: status=error, error=ClassName."""
        with patch.object(
            junos_upgrade,
            "_compute_local_checksum",
            side_effect=OSError("IO broken"),
        ):
            result = junos_upgrade.check_local_package("test-host", self._dev())
        assert result["status"] == "error"
        assert result["error"] == "OSError"
        assert capsys.readouterr().out == ""


# -------------------------------------------------------------------
# check_remote_package
# -------------------------------------------------------------------


class TestCheckRemotePackage:
    def _dev(self):
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        return dev

    def test_cache_hit(self, junos_upgrade, mock_args, mock_config, capsys):
        junos_upgrade.set_hashcache("test-host", "junos-arm-32-22.4R3-S6.5.tgz", "abc123def456")
        with patch.object(junos_upgrade, "SW") as MockSW:
            result = junos_upgrade.check_remote_package("test-host", self._dev())
        assert result["status"] == "ok"
        assert result["cached"] is True
        assert result["remote_path"] == "/var/tmp"
        MockSW.return_value.remote_checksum.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_checksum_ok(self, junos_upgrade, mock_args, mock_config, capsys):
        mock_sw = MagicMock()
        mock_sw.remote_checksum.return_value = "abc123def456"
        with patch.object(junos_upgrade, "SW", return_value=mock_sw):
            result = junos_upgrade.check_remote_package("test-host", self._dev())
        assert result["status"] == "ok"
        assert result["cached"] is False
        mock_sw.remote_checksum.assert_called_once_with(
            "/var/tmp/junos-arm-32-22.4R3-S6.5.tgz", algorithm="md5"
        )
        assert capsys.readouterr().out == ""

    def test_file_missing(self, junos_upgrade, mock_args, mock_config, capsys):
        """remote_checksum returns None when the file is not on the device."""
        mock_sw = MagicMock()
        mock_sw.remote_checksum.return_value = None
        with patch.object(junos_upgrade, "SW", return_value=mock_sw):
            result = junos_upgrade.check_remote_package("test-host", self._dev())
        assert result["status"] == "missing"
        assert result["actual_hash"] is None
        assert "is not found" in result["message"]
        assert capsys.readouterr().out == ""

    def test_checksum_bad(self, junos_upgrade, mock_args, mock_config, capsys):
        mock_sw = MagicMock()
        mock_sw.remote_checksum.return_value = "WRONG"
        with patch.object(junos_upgrade, "SW", return_value=mock_sw):
            result = junos_upgrade.check_remote_package("test-host", self._dev())
        assert result["status"] == "bad"
        assert "BAD" in result["message"]
        assert capsys.readouterr().out == ""

    def test_rpc_error(self, junos_upgrade, mock_args, mock_config, capsys):
        mock_sw = MagicMock()
        mock_sw.remote_checksum.side_effect = RpcError()
        with patch.object(junos_upgrade, "SW", return_value=mock_sw):
            result = junos_upgrade.check_remote_package("test-host", self._dev())
        assert result["status"] == "error"
        assert result["error"] == "RpcError"
        assert capsys.readouterr().out == ""


# -------------------------------------------------------------------
# clear_reboot
# -------------------------------------------------------------------


class TestClearReboot:
    def test_dry_run(self, junos_upgrade, mock_args, capsys):
        mock_args.dry_run = True
        dev = MagicMock()
        result = junos_upgrade.clear_reboot(dev)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert "dry-run" in result["message"]
        dev.rpc.clear_reboot.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_success(self, junos_upgrade, mock_args, capsys):
        from lxml import etree
        dev = MagicMock()
        dev.rpc.clear_reboot.return_value = etree.fromstring(
            "<output>No shutdown/reboot scheduled.</output>"
        )
        result = junos_upgrade.clear_reboot(dev)
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert "successful" in result["message"]
        assert result["error"] is None
        assert capsys.readouterr().out == ""

    def test_terminating(self, junos_upgrade, mock_args, capsys):
        """`Terminating...` text also means success."""
        from lxml import etree
        dev = MagicMock()
        dev.rpc.clear_reboot.return_value = etree.fromstring(
            "<output>Terminating...</output>"
        )
        result = junos_upgrade.clear_reboot(dev)
        assert result["ok"] is True
        assert capsys.readouterr().out == ""

    def test_unrecognised_response(self, junos_upgrade, mock_args, capsys):
        from lxml import etree
        dev = MagicMock()
        dev.rpc.clear_reboot.return_value = etree.fromstring(
            "<output>something else</output>"
        )
        result = junos_upgrade.clear_reboot(dev)
        assert result["ok"] is False
        assert "failed" in result["message"]
        assert capsys.readouterr().out == ""

    def test_rpc_error(self, junos_upgrade, mock_args, capsys):
        dev = MagicMock()
        dev.rpc.clear_reboot.side_effect = RpcError()
        result = junos_upgrade.clear_reboot(dev)
        assert result["ok"] is False
        assert result["error"] == "RpcError"
        assert capsys.readouterr().out == ""
