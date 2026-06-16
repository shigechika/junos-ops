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
import configparser
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
        json=False,
        config="config.ini",
        tags=None,
        exclude_tags=None,
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


class TestFetchModelCheap:
    def test_parses_product_model(self):
        from lxml import etree
        dev = MagicMock()
        dev.rpc.get_software_information.return_value = etree.fromstring(
            "<software-information><product-model>MX5-T</product-model>"
            "<host-name>rt1</host-name></software-information>"
        )
        assert cli._fetch_model_cheap(dev) == "MX5-T"

    def test_returns_none_on_rpc_error(self):
        dev = MagicMock()
        dev.rpc.get_software_information.side_effect = RuntimeError("boom")
        assert cli._fetch_model_cheap(dev) is None

    def test_returns_none_when_field_missing(self):
        from lxml import etree
        dev = MagicMock()
        dev.rpc.get_software_information.return_value = etree.fromstring(
            "<software-information><host-name>rt1</host-name></software-information>"
        )
        assert cli._fetch_model_cheap(dev) is None


class TestCheckHostWorker:
    def test_connect_only_uses_cheap_model_rpc(self, mock_config):
        """--connect uses the single get-software-information RPC, not full facts."""
        common.args = _make_check_args(check_connect=True)
        mock_dev = MagicMock()
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
        ), patch.object(
            cli, "_fetch_model_cheap", return_value="MX5-T"
        ) as mock_cheap:
            result = cli._check_host("test-host")
        assert result["connect"]["ok"] is True
        assert result["remote"] is None
        assert result["model"] == "MX5-T"
        assert result["model_source"] == "device"
        mock_cheap.assert_called_once_with(mock_dev)
        # Full facts collection must NOT be triggered.
        mock_dev.facts.get.assert_not_called()
        mock_dev.close.assert_called_once()

    def test_connect_plus_remote_fetches_model_from_facts(self, mock_config):
        """--remote needs the model → facts access is allowed."""
        common.args = _make_check_args(check_connect=True, check_remote=True)
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
        ), patch.object(
            junos_upgrade_mod,
            "check_remote_package_by_model",
            return_value={"status": "ok", "file": "pkg", "cached": False},
        ):
            result = cli._check_host("test-host")
        assert result["model"] == "EX2300-24T"
        assert result["model_source"] == "device"

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


def _make_inventory_config():
    """Build a config with model-tagged hosts for inventory-filter tests."""
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.read_dict(
        {
            "DEFAULT": {
                "id": "u", "pw": "p", "sshkey": "k",
                "port": "830", "hashalgo": "md5", "rpath": "/var/tmp",
                "ex2300-24t.file": "junos-arm-32.tgz",
                "ex2300-24t.hash": "h1",
                "ex3400-24t.file": "junos-arm-32-ex3400.tgz",
                "ex3400-24t.hash": "h2",
                "srx345.file": "junos-srxsme.tgz",
                "srx345.hash": "h3",
            },
            "rt-ex2300": {
                "host": "rt-ex2300", "tags": "main", "model": "EX2300-24T",
            },
            "rt-ex3400": {
                "host": "rt-ex3400", "tags": "main", "model": "EX3400-24T",
            },
            "rt-srx345": {
                "host": "rt-srx345", "tags": "main", "model": "SRX345",
            },
            "rt-lab": {
                "host": "rt-lab", "tags": "lab", "model": "EX2300-24T",
            },
            "rt-no-model": {"host": "rt-no-model", "tags": "main"},
        }
    )
    return cfg


class TestCheckLocalInventoryHostFilter:
    """check --local in filter mode (--tags / --exclude-tags / hostnames)."""

    def _patched_check(self):
        return patch.object(
            junos_upgrade_mod,
            "check_local_package_by_model",
            side_effect=lambda h, m: {
                "file": f"pkg-{m}.tgz",
                "local_file": f"/fw/pkg-{m}.tgz",
                "status": "ok",
                "cached": False,
                "actual_hash": "x",
                "expected_hash": "x",
                "message": "",
                "error": None,
            },
        )

    def test_tags_filter_restricts_to_host_models(self, junos_common):
        """--tags main: inventory limited to models used by main hosts."""
        common.config = _make_inventory_config()
        common.args = _make_check_args(check_local=True, tags="main")
        with self._patched_check() as mock_chk:
            rows = cli._check_local_inventory()
        models = [c.args[1] for c in mock_chk.call_args_list]
        # main hosts: rt-ex2300, rt-ex3400, rt-srx345, rt-no-model.
        # rt-no-model has no [host].model -> unmapped row, model dropped.
        assert sorted(models) == ["ex2300-24t", "ex3400-24t", "srx345"]
        unmapped = [r for r in rows if r["status"] == "unmapped"]
        assert len(unmapped) == 1
        assert unmapped[0]["hostname"] == "rt-no-model"

    def test_tags_excludes_drops_model(self, junos_common):
        """--tags main --exclude-tags drop: drop-tagged host removes its model."""
        common.config = _make_inventory_config()
        # Tag-based exclude: tag srx host so we can drop it via exclude-tags.
        common.config.set("rt-srx345", "tags", "main, drop")
        common.args = _make_check_args(
            check_local=True, tags="main", exclude_tags="drop",
        )
        with self._patched_check() as mock_chk:
            cli._check_local_inventory()
        models = [c.args[1] for c in mock_chk.call_args_list]
        assert "srx345" not in models
        assert sorted(models) == ["ex2300-24t", "ex3400-24t"]

    def test_hostnames_filter(self, junos_common):
        """Explicit hostnames also narrow the inventory."""
        common.config = _make_inventory_config()
        common.args = _make_check_args(
            check_local=True, specialhosts=["rt-ex2300"],
        )
        with self._patched_check() as mock_chk:
            cli._check_local_inventory()
        models = [c.args[1] for c in mock_chk.call_args_list]
        assert models == ["ex2300-24t"]

    def test_model_and_tag_intersect(self, junos_common):
        """--model X --tags main: intersection (only if X is in main hosts)."""
        common.config = _make_inventory_config()
        common.args = _make_check_args(
            check_local=True, tags="main", check_model="EX2300-24T",
        )
        with self._patched_check() as mock_chk:
            cli._check_local_inventory()
        models = [c.args[1] for c in mock_chk.call_args_list]
        assert models == ["ex2300-24t"]

    def test_model_filter_outside_tag_set_is_empty(self, junos_common, caplog):
        """--model M not in the host-filtered set yields zero rows + an info log."""
        common.config = _make_inventory_config()
        # Only lab has rt-lab (ex2300). Ask for SRX345 -> empty intersection.
        common.args = _make_check_args(
            check_local=True, tags="lab", check_model="SRX345",
        )
        with self._patched_check() as mock_chk:
            with caplog.at_level("INFO", logger="junos_ops.cli"):
                rows = cli._check_local_inventory()
        assert mock_chk.call_count == 0
        # No model rows; lab host has model so no unmapped either.
        assert rows == []
        # Operator gets told *why* zero rows came back instead of guessing.
        assert any(
            "no models matched after filtering" in m for m in caplog.messages
        )

    def test_unmapped_host_emits_row(self, junos_common):
        """Selected host without [host].model surfaces an unmapped inventory row."""
        common.config = _make_inventory_config()
        common.args = _make_check_args(
            check_local=True, specialhosts=["rt-no-model"],
        )
        with self._patched_check():
            rows = cli._check_local_inventory()
        # No model resolvable -> only the unmapped row.
        assert len(rows) == 1
        assert rows[0]["status"] == "unmapped"
        assert rows[0]["hostname"] == "rt-no-model"

    def test_default_mode_unchanged(self, junos_common):
        """No filter: existing behaviour (every <model>.file in DEFAULT)."""
        common.config = _make_inventory_config()
        common.args = _make_check_args(check_local=True)
        with self._patched_check() as mock_chk:
            cli._check_local_inventory()
        models = sorted(c.args[1] for c in mock_chk.call_args_list)
        # All three configured models, including srx345 which no main host
        # is wearing in the filtered scenarios above.
        assert models == ["ex2300-24t", "ex3400-24t", "srx345"]

    def test_format_inventory_with_lpath_header(self):
        """Shared lpath is shown once above the table, not repeated per row."""
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
        lines = out.splitlines()
        assert lines[0] == "lpath: /opt/fw"
        assert lines[1].startswith("model")
        # local_file column is dropped.
        assert "local_file" not in out
        assert "ok(cached)" in out
        assert "missing" in out  # status column shows it
        # 'missing' has no detail line (status + file column already convey it).
        assert "mx5-t:" not in out

    def test_format_inventory_without_lpath(self):
        """When lpath is unset (local_file == file), no lpath header is emitted."""
        rows = [
            {
                "model": "ex2300-24t",
                "file": "junos-arm.tgz",
                "local_file": "junos-arm.tgz",
                "status": "ok",
                "cached": False,
            },
        ]
        out = display.format_check_local_inventory(rows)
        assert not out.startswith("lpath:")
        assert out.splitlines()[0].startswith("model")


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
        assert "missing" in out  # status column shows it
        assert "jinstall-ppc.tgz" in out
        # 'missing' alone no longer triggers a detail line (redundant).
        assert "rt2: remote:" not in out

    def test_empty_rows_renders_header(self):
        out = display.format_check_table(
            [], show_connect=True, show_local=False, show_remote=False
        )
        assert out.startswith("hostname")
        assert "connect" in out

    def test_disk_column_shown_when_requested(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "EX2300-24T",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
                "disk": {"ok": True, "avail_mib": 800, "filesystem": "/var/tmp"},
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_remote=False, show_disk=True
        )
        assert "avail" in out
        assert "800 MiB" in out

    def test_disk_column_warning_marker(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "EX2300-24T",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
                "disk": {"ok": True, "avail_mib": 400, "filesystem": "/var/tmp"},
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_disk=True
        )
        assert "!400 MiB" in out

    def test_disk_column_gib_unit(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "MX240",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
                "disk": {"ok": True, "avail_mib": 2048, "filesystem": "/var/tmp"},
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_disk=True
        )
        assert "2.0 GiB" in out

    def test_disk_column_hidden_when_not_requested(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "MX5-T",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
                "disk": {"ok": True, "avail_mib": 500, "filesystem": "/var/tmp"},
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_disk=False
        )
        assert "avail" not in out
        assert "MiB" not in out

    def test_disk_column_dash_on_failure(self):
        rows = [
            {
                "hostname": "rt1",
                "model": "MX5-T",
                "connect": {"ok": True, "message": "connected"},
                "local": None,
                "remote": None,
                "disk": {"ok": False, "avail_mib": None, "error": "RPC failed"},
            },
        ]
        out = display.format_check_table(
            rows, show_connect=True, show_disk=True
        )
        assert "avail" in out
        lines = out.splitlines()
        data_line = [l for l in lines if "rt1" in l][0]
        assert "  -  " in data_line or data_line.endswith("  -")


# -------------------------------------------------------------------
# upgrade.get_disk_avail
# -------------------------------------------------------------------


class TestGetDiskAvail:
    def _make_xml(self, entries):
        """Build a get-system-storage-information XML with given entries.

        ``entries`` is a list of (mounted_on, avail_blocks) tuples.
        """
        from lxml import etree

        root = etree.Element("system-storage-information")
        for mounted, avail in entries:
            fs = etree.SubElement(root, "filesystem")
            etree.SubElement(fs, "mounted-on").text = mounted
            etree.SubElement(fs, "available-blocks").text = str(avail)
        return root

    def test_selects_most_specific_mount(self, mock_config):
        from junos_ops import upgrade

        dev = MagicMock()
        dev.rpc.get_system_storage_information.return_value = self._make_xml([
            ("/", 2097152),   # 2 GiB
            ("/var", 1048576),  # 1 GiB
            ("/var/tmp", 614400),  # 600 MiB — most specific for rpath=/var/tmp
        ])
        result = upgrade.get_disk_avail("test-host", dev)
        assert result["ok"] is True
        assert result["filesystem"] == "/var/tmp"
        # 614400 KiB // 1024 == 600 MiB
        assert result["avail_mib"] == 600

    def test_falls_back_to_parent_mount(self, mock_config):
        from junos_ops import upgrade

        dev = MagicMock()
        dev.rpc.get_system_storage_information.return_value = self._make_xml([
            ("/", 2097152),   # 2 GiB
            ("/var", 1048576),  # 1 GiB
        ])
        result = upgrade.get_disk_avail("test-host", dev)
        assert result["ok"] is True
        assert result["filesystem"] == "/var"
        assert result["avail_mib"] == 1024  # 1048576 KiB // 1024 == 1024 MiB == 1 GiB

    def test_rpc_error_sets_ok_false(self, mock_config):
        from junos_ops import upgrade

        dev = MagicMock()
        dev.rpc.get_system_storage_information.side_effect = RuntimeError("boom")
        result = upgrade.get_disk_avail("test-host", dev)
        assert result["ok"] is False
        assert result["avail_mib"] is None
        assert "boom" in result["error"]

    def test_check_host_populates_disk(self, mock_config):
        """_check_host sets result['disk'] when connected."""
        from junos_ops import upgrade

        common.args = _make_check_args(check_connect=True)
        mock_dev = MagicMock()
        disk_result = {"ok": True, "avail_mib": 700, "filesystem": "/var/tmp"}
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
        ), patch.object(
            cli, "_fetch_model_cheap", return_value="EX2300-24T"
        ), patch.object(
            upgrade, "get_disk_avail", return_value=disk_result
        ) as mock_disk:
            result = cli._check_host("test-host")
        assert result["disk"] == disk_result
        mock_disk.assert_called_once_with("test-host", mock_dev)

    def test_check_host_disk_none_on_connect_fail(self, mock_config):
        """_check_host leaves result['disk'] as None when connection fails."""
        common.args = _make_check_args(check_connect=True)
        with patch.object(
            common, "connect",
            return_value={
                "hostname": "test-host",
                "host": "192.0.2.1",
                "ok": False,
                "dev": None,
                "error": "ConnectTimeoutError",
                "error_message": "timeout",
            },
        ):
            result = cli._check_host("test-host")
        assert result["disk"] is None

    def test_no_matching_filesystem(self, mock_config):
        """Returns ok=False silently when no mount point covers rpath."""
        from junos_ops import upgrade

        dev = MagicMock()
        dev.rpc.get_system_storage_information.return_value = self._make_xml([
            ("/mnt/other", 1048576),  # 1 GiB, but not a prefix of /var/tmp
        ])
        result = upgrade.get_disk_avail("test-host", dev)
        assert result["ok"] is False
        assert result["avail_mib"] is None
        assert result["error"] is None

    def test_path_boundary_no_partial_match(self, mock_config):
        """A mount at /var/t must not match rpath=/var/tmp."""
        from junos_ops import upgrade

        dev = MagicMock()
        dev.rpc.get_system_storage_information.return_value = self._make_xml([
            ("/", 2097152),    # 2 GiB
            ("/var/t", 524288),   # 512 MiB — partial prefix, should NOT win
            ("/var/tmp", 614400),  # 600 MiB — exact match, should win
        ])
        result = upgrade.get_disk_avail("test-host", dev)
        assert result["ok"] is True
        assert result["filesystem"] == "/var/tmp"
        assert result["avail_mib"] == 600
