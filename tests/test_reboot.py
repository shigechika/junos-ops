"""reboot 関連関数のテスト"""

import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from lxml import etree


class TestCheckAndReinstall:
    """check_and_reinstall() のテスト"""

    def test_no_pending(self, junos_upgrade, mock_args, mock_config):
        """pending version なし → 再インストールしない"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value=None):
            result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True

    def test_config_not_changed(self, junos_upgrade, mock_args, mock_config):
        """コミット時刻 <= rescue 時刻 → 再インストールしない"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=2000):
                    result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True

    def test_config_equal_time(self, junos_upgrade, mock_args, mock_config):
        """コミット時刻 == rescue 時刻 → 再インストールしない"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True

    def test_config_changed(self, junos_upgrade, mock_args, mock_config):
        """コミット時刻 > rescue 時刻 → 再インストール実行"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_sw = MagicMock()
        mock_sw.install.return_value = (True, "install ok")
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = True
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                        with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                            result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        mock_cu.rescue.assert_called_once_with("save")
        mock_sw.install.assert_called_once()

    def test_no_rescue_file(self, junos_upgrade, mock_args, mock_config):
        """rescue ファイルなし → 再インストール実行"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_sw = MagicMock()
        mock_sw.install.return_value = (True, "install ok")
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = True
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                        with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                            result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        mock_cu.rescue.assert_called_once_with("save")
        mock_sw.install.assert_called_once()

    def test_dry_run(self, junos_upgrade, mock_args, mock_config):
        """dry-run 時はメッセージのみ、再インストールしない"""
        mock_args.dry_run = True
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True

    def test_install_failure(self, junos_upgrade, mock_args, mock_config):
        """再インストール失敗 → True を返す"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_sw = MagicMock()
        mock_sw.install.return_value = (False, "install failed")
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = True
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                        with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                            result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is False

    def test_pending_current_primary_skip(self, junos_upgrade, mock_args, mock_config):
        """pending_install_epoch >= commit_epoch → primary skip (issue #54 fundamental fix)"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=2000):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "pending_current"
        assert result["reinstalled"] is False
        # rescue save 不要（SW.install も呼ばれていないはず）
        assert result["rescue_save"] is None

    def test_pending_current_takes_priority_over_rescue(self, junos_upgrade, mock_args, mock_config):
        """pending_install_epoch が利用できれば rescue_epoch より優先"""
        dev = MagicMock()
        # commit(1500) > rescue(1000) だが pending_install(2000) はより新しい → skip
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1500, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=2000):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["skipped"] is True
        assert result["skip_reason"] == "pending_current"

    def test_rescue_fallback_when_pending_unknown(self, junos_upgrade, mock_args, mock_config):
        """get_pending_install_time が None → rescue_epoch に fallback"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=2000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=None):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["skipped"] is True
        # fallback path は legacy の reason を保持
        assert result["skip_reason"] == "config_unchanged"

    def test_already_pending_treated_as_skip(self, junos_upgrade, mock_args, mock_config):
        """`already an install pending` は再インストール不要の skip として扱う (issue #54 安全網)"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_sw = MagicMock()
        mock_sw.install.return_value = (
            False,
            "Package validation failed\n"
            "ERROR: There is already an install pending.\n"
            "ERROR:     Use the 'request system reboot' command to complete the install,\n"
            "ERROR:     or the 'request system software rollback' command to back it out.\n",
        )
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = True
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=None):
                        with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                            with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                                result = junos_upgrade.check_and_reinstall("test-host", dev)
        # reboot should still be allowed to proceed.
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "already_pending"
        assert result["reinstalled"] is False
        assert result["error"] is None
        assert any(
            "already pending" in step.get("message", "") for step in result["steps"]
        )

    def test_drift_warning_when_commit_newer_than_pending(self, junos_upgrade, mock_args, mock_config):
        """commit > pending_install の状態で already_pending → config drift 警告付き skip"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_sw = MagicMock()
        mock_sw.install.return_value = (
            False,
            "Package validation failed\nERROR: There is already an install pending.\n",
        )
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = True
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(3000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=1000):
                        with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                            with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                                result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "already_pending"
        # drift 警告が含まれる
        assert any(
            "stale embedded config" in step.get("message", "") for step in result["steps"]
        )

    def test_rescue_save_failure(self, junos_upgrade, mock_args, mock_config):
        """rescue config 保存失敗 → True を返す"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        mock_cu = MagicMock()
        mock_cu.rescue.return_value = False
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is False

    def test_no_commit_info(self, junos_upgrade, mock_args, mock_config):
        """コミット情報取得失敗 → スキップ"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=None):
                result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True


class TestRebootWithReinstall:
    """reboot() が check_and_reinstall() を呼ぶことを確認"""

    def _make_reboot_xml(self, text="No shutdown/reboot scheduled.\n"):
        """テスト用の reboot information XML を生成する"""
        root = etree.Element("output")
        root.text = text
        return root

    def test_reboot_calls_check_and_reinstall(self, junos_upgrade, mock_args, mock_config):
        """reboot() が check_and_reinstall() を呼ぶ"""
        dev = MagicMock()
        dev.rpc.get_reboot_information.return_value = self._make_reboot_xml()
        mock_sw = MagicMock()
        mock_sw.reboot.return_value = "Shutdown at Fri Jun 13 05:00:00 2025. [pid 97978]"
        reboot_dt = datetime.datetime(2025, 6, 13, 5, 0)
        with patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}) as mock_check:
            with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                result = junos_upgrade.reboot("test-host", dev, reboot_dt)
        assert result["code"] == 0
        assert result["ok"] is True
        mock_check.assert_called_once_with("test-host", dev)

    def test_reboot_reinstall_failure(self, junos_upgrade, mock_args, mock_config):
        """check_and_reinstall() 失敗時に reboot() が 6 を返す"""
        dev = MagicMock()
        dev.rpc.get_reboot_information.return_value = self._make_reboot_xml()
        reboot_dt = datetime.datetime(2025, 6, 13, 5, 0)
        with patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": False, "steps": [], "error": "reinstall_failed"}):
            result = junos_upgrade.reboot("test-host", dev, reboot_dt)
        assert result["code"] == 6
        assert result["ok"] is False


class TestDeleteSnapshots:
    """delete_snapshots() は dict を返す"""

    def test_switch_personality(self, junos_upgrade, mock_args, capsys):
        """personality=SWITCH で RPC が呼ばれる"""
        dev = MagicMock()
        dev.facts = {"personality": "SWITCH"}
        # Return a real lxml element so etree.tostring() succeeds.
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        result = junos_upgrade.delete_snapshots(dev)
        assert result["applied"] is True
        assert result["ok"] is True
        assert result["error"] is None
        # Call style is positional dict to bypass the kwarg bool-coercion bug
        # that recent PyEZ hits with delete="*".
        dev.rpc.request_snapshot.assert_called_once_with(
            {"delete": "*"}, dev_timeout=60
        )
        # core は print しない
        assert capsys.readouterr().out == ""

    def test_non_switch_personality(self, junos_upgrade, mock_args, capsys):
        """personality=MX では RPC が呼ばれず applied=False"""
        dev = MagicMock()
        dev.facts = {"personality": "MX"}
        result = junos_upgrade.delete_snapshots(dev)
        assert result["applied"] is False
        assert result["ok"] is True
        dev.rpc.request_snapshot.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_dry_run(self, junos_upgrade, mock_args, capsys):
        """dry-run 時は RPC が呼ばれず applied=True, dry_run=True"""
        mock_args.dry_run = True
        dev = MagicMock()
        dev.facts = {"personality": "SWITCH"}
        result = junos_upgrade.delete_snapshots(dev)
        assert result["applied"] is True
        assert result["dry_run"] is True
        assert result["ok"] is True
        assert "dry-run" in result["message"]
        dev.rpc.request_snapshot.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_rpc_error_non_fatal(self, junos_upgrade, mock_args, capsys):
        """RPC エラーは ok=True で保持（致命的でない）、error に例外名"""
        from jnpr.junos.exception import RpcError
        dev = MagicMock()
        dev.facts = {"personality": "SWITCH"}
        dev.rpc.request_snapshot.side_effect = RpcError()
        result = junos_upgrade.delete_snapshots(dev)
        assert result["applied"] is True
        assert result["ok"] is True
        assert result["error"] == "RpcError"
        assert "skipped" in result["message"]
        assert capsys.readouterr().out == ""
