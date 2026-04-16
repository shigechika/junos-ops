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
