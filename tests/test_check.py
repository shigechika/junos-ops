"""Tests for the ``check`` subcommand (pre-flight verification).

Covers three surfaces:

1. ``upgrade.check_local_package_by_model`` — the device-less local
   checksum helper that ``check --local`` uses.
2. ``upgrade.check_remote_package_by_model`` — the by-model variant
   that ``check --remote`` uses when the model comes from
   ``config.ini`` or ``--model``.
3. ``cli._check_host`` — the worker that dispatches the requested
   subset of checks and returns a result dict for the table renderer.
4. ``display.format_check_table`` — column selection, status labels,
   and failure-detail rendering.
"""

import argparse
import hashlib
import os
import tempfile
from unittest.mock import MagicMock, patch

from junos_ops import cli
from junos_ops import common
from junos_ops import display
from junos_ops import upgrade as junos_upgrade_mod


# -------------------------------------------------------------------
# check_local_package_by_model
# -------------------------------------------------------------------


class TestCheckLocalPackageByModel:
    def test_ok_with_real_file(self, mock_args, mock_config, tmp_path, capsys):
        """Creates a real file, computes md5, verifies status=ok."""
        pkg = tmp_path / "junos-arm-32-22.4R3-S6.5.tgz"
        pkg.write_bytes(b"firmware-bytes")
        expected = hashlib.md5(b"firmware-bytes").hexdigest()
        common.config.set("DEFAULT", "lpath", str(tmp_path))
        common.config.set("DEFAULT", "ex2300-24t.hash", expected)

        result = junos_upgrade_mod.check_local_package_by_model(
            "test-host", "EX2300-24T"
        )
        assert result["status"] == "ok"
        assert result["cached"] is False
        assert result["actual_hash"] == expected
        assert capsys.readouterr().out == ""

    def test_missing_file(self, mock_args, mock_config, tmp_path, capsys):
        common.config.set("DEFAULT", "lpath", str(tmp_path))
        result = junos_upgrade_mod.check_local_package_by_model(
            "test-host", "EX2300-24T"
        )
        assert result["status"] == "missing"
        assert result["actual_hash"] is None
        assert capsys.readouterr().out == ""

    def test_bad_checksum(self, mock_args, mock_config, tmp_path, capsys):
        pkg = tmp_path / "junos-arm-32-22.4R3-S6.5.tgz"
        pkg.write_bytes(b"other-bytes")
        common.config.set("DEFAULT", "lpath", str(tmp_path))
        # Expected hash in mock_config is "abc123def456" — won't match.
        result = junos_upgrade_mod.check_local_package_by_model(
            "test-host", "EX2300-24T"
        )
        assert result["status"] == "bad"
        assert result["actual_hash"] != "abc123def456"
        assert capsys.readouterr().out == ""


# -------------------------------------------------------------------
# check_remote_package_by_model
# -------------------------------------------------------------------


class TestCheckRemotePackageByModel:
    def test_delegation(self, mock_args, mock_config, capsys):
        dev = MagicMock()
        # facts NOT accessed when model is supplied explicitly.
        dev.facts = {"model": "should-not-be-read"}
        mock_sw = MagicMock()
        mock_sw.remote_checksum.return_value = "abc123def456"
        with patch.object(junos_upgrade_mod, "SW", return_value=mock_sw):
            result = junos_upgrade_mod.check_remote_package_by_model(
                "test-host", dev, "EX2300-24T"
            )
        assert result["status"] == "ok"
        mock_sw.remote_checksum.assert_called_once_with(
            "/var/tmp/junos-arm-32-22.4R3-S6.5.tgz", algorithm="md5"
        )
        assert capsys.readouterr().out == ""


# -------------------------------------------------------------------
# cli._check_host
# -------------------------------------------------------------------


def _make_check_args(**overrides):
    base = argparse.Namespace(
        debug=False,
        dry_run=False,
        force=False,
        config="config.ini",
        tags=None,
        workers=1,
        specialhosts=[],
        check_connect=False,
        check_local=False,
        check_remote=False,
        check_all=False,
        check_model=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestCheckHostWorker:
    def test_connect_only_ok(self, mock_config):
        common.args = _make_check_args(check_connect=True)
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(
            common, "connect",
            return_value={
                "hostname": "test-host",
                "host": "192.0.2.1",
                "ok": True,
                "dev": mock_dev,
                "error": None,
                "error_message": None,
            },
        ):
            result = cli._check_host("test-host")
        assert result["connect"]["ok"] is True
        assert result["remote"] is None
        assert result["model"] == "EX2300-24T"
        assert result["model_source"] == "device"
        mock_dev.close.assert_called_once()

    def test_connect_fail(self, mock_config):
        common.args = _make_check_args(check_connect=True)
        with patch.object(
            common, "connect",
            return_value={
                "hostname": "test-host",
                "host": "192.0.2.1",
                "ok": False,
                "dev": None,
                "error": "ConnectTimeoutError",
                "error_message": "Connection timeout",
            },
        ):
            result = cli._check_host("test-host")
        assert result["connect"]["ok"] is False
        assert result["connect"]["error"] == "ConnectTimeoutError"
        assert result["model"] is None

    def test_remote_unchecked_when_connect_fails(self, mock_config):
        common.args = _make_check_args(
            check_connect=True, check_remote=True, check_model="EX2300-24T"
        )
        with patch.object(
            common, "connect",
            return_value={
                "hostname": "test-host",
                "host": "192.0.2.1",
                "ok": False,
                "dev": None,
                "error": "ConnectRefusedError",
                "error_message": "refused",
            },
        ):
            result = cli._check_host("test-host")
        assert result["connect"]["ok"] is False
        assert result["remote"]["status"] == "unchecked"
        assert result["remote"]["message"] == "not connected"


# -------------------------------------------------------------------
# display.format_check_table
# -------------------------------------------------------------------


class TestCheckLocalInventory:
    def test_iterates_default_models(self, mock_config, tmp_path):
        """All `<model>.file` pairs in DEFAULT are checked, regardless of host list."""
        common.config.set("DEFAULT", "lpath", str(tmp_path))
        common.config.set("DEFAULT", "ex4300-32f.file", "pkg-ex4300.tgz")
        common.config.set("DEFAULT", "ex4300-32f.hash", "deadbeef")
        common.args = _make_check_args(check_local=True)

        with patch.object(
            junos_upgrade_mod,
            "check_local_package_by_model",
            side_effect=lambda h, m: {
                "file": f"pkg-{m}.tgz",
                "local_file": f"{tmp_path}/pkg-{m}.tgz",
                "status": "missing",
                "cached": False,
                "actual_hash": None,
                "expected_hash": "deadbeef",
                "message": f"  - local package: {tmp_path}/pkg-{m}.tgz is not found.",
                "error": None,
            },
        ) as mock_chk:
            rows = cli._check_local_inventory()

        models_checked = [call.args[1] for call in mock_chk.call_args_list]
        assert "ex2300-24t" in models_checked
        assert "ex4300-32f" in models_checked
        assert all(r["status"] == "missing" for r in rows)
        # Section passed to the core is always "DEFAULT" in inventory mode.
        assert all(call.args[0] == "DEFAULT" for call in mock_chk.call_args_list)

    def test_model_filter(self, mock_config):
        """--model X restricts inventory to the requested model only."""
        common.args = _make_check_args(check_local=True, check_model="EX2300-24T")
        with patch.object(
            junos_upgrade_mod,
            "check_local_package_by_model",
            return_value={"status": "ok", "file": "pkg", "cached": False},
        ) as mock_chk:
            rows = cli._check_local_inventory()
        mock_chk.assert_called_once_with("DEFAULT", "EX2300-24T")
        assert len(rows) == 1

    def test_format_inventory(self):
        rows = [
            {
                "model": "ex2300-24t",
                "file": "junos-arm.tgz",
                "local_file": "/opt/fw/junos-arm.tgz",
                "status": "ok",
                "cached": True,
            },
            {
                "model": "mx5-t",
                "file": "jinstall-ppc.tgz",
                "local_file": "/opt/fw/jinstall-ppc.tgz",
                "status": "missing",
                "cached": False,
                "message": "  - local package: /opt/fw/jinstall-ppc.tgz is not found.",
            },
        ]
        out = display.format_check_local_inventory(rows)
        assert "model" in out.splitlines()[0]
        assert "ok(cached)" in out
        assert "missing" in out
        # Detail line for missing entry surfaces the filename.
        assert "mx5-t:" in out
        assert "is not found" in out


class TestFormatCheckTable:
    def test_connect_only_columns(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "MX5-T",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
            },
            {
                "hostname": "rt2",
                "model": None,
                "connect": {"ok": False, "message": "Connection timeout"},
                "local": None,
                "remote": None,
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_local=False, show_remote=False
        )
        lines = out.splitlines()
        # Header + separator + 2 rows + blank + 1 detail line = 6
        assert lines[0].startswith("hostname")
        assert "connect" in lines[0]
        assert "local" not in lines[0]
        assert "remote" not in lines[0]
        assert "rt1" in out and "ok" in out
        assert "rt2" in out and "fail" in out
        assert "Connection timeout" in out  # detail block

    def test_all_columns_with_cached_and_failures(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "MX5-T",
                "connect": {"ok": True, "message": "connected"},
                "local": {
                    "status": "ok",
                    "cached": True,
                    "file": "jinstall-ppc.tgz",
                    "message": "",
                },
                "remote": {
                    "status": "ok",
                    "cached": False,
                    "file": "jinstall-ppc.tgz",
                    "message": "",
                },
            },
            {
                "hostname": "rt2",
                "model": "EX2300",
                "connect": {"ok": True, "message": "connected"},
                "local": {
                    "status": "ok",
                    "cached": False,
                    "file": "junos-arm.tgz",
                    "message": "",
                },
                "remote": {
                    "status": "missing",
                    "cached": False,
                    "file": "junos-arm.tgz",
                    "message": "  - remote package: junos-arm.tgz is not found.",
                },
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_local=True, show_remote=True
        )
        assert "ok(cached)" in out
        assert "missing" in out
        assert "jinstall-ppc.tgz" in out
        assert "rt2: remote:" in out  # detail line

    def test_empty_rows_renders_header(self):
        out = display.format_check_table(
            [], show_connect=True, show_local=False, show_remote=False
        )
        assert out.startswith("hostname")
        assert "connect" in out
