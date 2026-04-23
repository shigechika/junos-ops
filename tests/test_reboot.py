"""reboot 関連関数のテスト"""

import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from lxml import etree


class TestCheckAndReinstall:
    """check_and_reinstall() のテスト

    Design after issue #57: check_and_reinstall は pending に対して re-install
    を試みない。JUNOS が pending 上に install を許さないためで、旧実装の
    「config が更新されたら再 install で pending を refresh」は常に失敗
    していた。今は diagnostic のみで、ok は常に True、skipped は常に True。
    """

    def test_no_pending(self, junos_upgrade, mock_args, mock_config):
        """pending version なし → skip_reason='no_pending'"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value=None):
            result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "no_pending"
        assert result["reinstalled"] is False

    def test_no_commit_info(self, junos_upgrade, mock_args, mock_config):
        """コミット情報取得失敗 → skip_reason='no_commit_info'"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=None):
                result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skip_reason"] == "no_commit_info"

    def test_pending_current_primary_skip(self, junos_upgrade, mock_args, mock_config):
        """commit_epoch <= pending_install_epoch → skip_reason='pending_current'"""
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
        assert result["drift_detected"] is False
        # 警告は出ない（embedded config が最新なので）
        assert len(result["steps"]) == 0

    def test_pending_current_takes_priority_over_rescue(self, junos_upgrade, mock_args, mock_config):
        """pending_install_epoch があれば rescue_epoch より優先して判定"""
        dev = MagicMock()
        # commit(1500) > rescue(1000) でも pending_install(2000) の方が新しい → skip
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1500, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=2000):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["skip_reason"] == "pending_current"

    def test_drift_detected(self, junos_upgrade, mock_args, mock_config):
        """commit_epoch > pending_install_epoch → drift 警告付き skip"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(3000, "2026-04-22 16:19", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=1000):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "drift_detected"
        assert result["drift_detected"] is True
        assert result["reinstalled"] is False
        # drift 警告が含まれる
        assert any(
            "older embedded config" in step.get("message", "")
            for step in result["steps"]
        )

    def test_rescue_fallback_when_pending_unknown(self, junos_upgrade, mock_args, mock_config):
        """pending_install_epoch が None → rescue_epoch に fallback"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(1000, "2001-01-01", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=2000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=None):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["skipped"] is True
        assert result["skip_reason"] == "config_unchanged"

    def test_rescue_fallback_commit_newer_warns(self, junos_upgrade, mock_args, mock_config):
        """pending_install 不明 + commit > rescue → 'cannot_verify' with soft warning"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="22.4R3-S6.5"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(3000, "2026-04-22", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=None):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        assert result["skip_reason"] == "cannot_verify"
        assert any(
            "rescue" in step.get("message", "").lower()
            for step in result["steps"]
        )

    def test_cannot_verify_neither_marker(self, junos_upgrade, mock_args, mock_config):
        """pending_install も rescue も不明（issue #54 / #57 の再現条件）→ cannot_verify + warning"""
        dev = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(2000, "2026-04-22", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=None):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=None):
                        result = junos_upgrade.check_and_reinstall("test-host", dev)
        # ok=True が最重要: issue #54 / #57 では install 失敗で False になっていた
        assert result["ok"] is True
        assert result["skip_reason"] == "cannot_verify"
        assert result["reinstalled"] is False
        # SW.install も rescue.save も呼ばれていない（pattern は patch しないことで暗黙に保証）

    def test_never_attempts_install(self, junos_upgrade, mock_args, mock_config):
        """どのパスでも SW.install は呼ばれない（設計方針の回帰ガード）"""
        dev = MagicMock()
        mock_sw = MagicMock()
        mock_cu = MagicMock()
        with patch.object(junos_upgrade, "get_pending_version", return_value="23.4R2-S7.4"):
            with patch.object(junos_upgrade, "get_commit_information", return_value=(3000, "2026-04-22", "admin", "cli")):
                with patch.object(junos_upgrade, "get_rescue_config_time", return_value=1000):
                    with patch.object(junos_upgrade, "get_pending_install_time", return_value=500):
                        with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                            with patch("junos_ops.upgrade.Config", return_value=mock_cu):
                                result = junos_upgrade.check_and_reinstall("test-host", dev)
        assert result["ok"] is True
        mock_sw.install.assert_not_called()
        mock_cu.rescue.assert_not_called()


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
