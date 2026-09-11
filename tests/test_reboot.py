"""reboot 関連関数のテスト"""

import datetime

import pytest
from unittest.mock import MagicMock, patch

from jnpr.junos.exception import RpcError
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

    def test_reboot_xml_parse_error_without_force(self, junos_upgrade, mock_args, mock_config):
        """get_reboot_information の XML parse エラー + --force なし → code=7 で fail (issue #60)

        code=7 を code=3 (clear_reboot_failed) と分離してある点を確認する —
        CLI exit code だけで両者を区別できるのが期待。
        """
        dev = MagicMock()
        dev.rpc.get_reboot_information.side_effect = etree.XMLSyntaxError(
            "Opening and ending tag mismatch: request-reboot-status line 3 and rpc-reply",
            None, 21, 13,
        )
        mock_args.force = False
        reboot_dt = datetime.datetime(2025, 6, 13, 5, 0)
        result = junos_upgrade.reboot("test-host", dev, reboot_dt)
        assert result["code"] == 7
        assert result["ok"] is False
        assert result["error"] == "get_reboot_information_parse_error"
        # --force の案内メッセージが含まれる
        assert "--force" in (result.get("message") or "")
        assert any(
            "cannot parse" in step.get("message", "") for step in result["steps"]
        )

    def test_reboot_xml_parse_error_with_force(self, junos_upgrade, mock_args, mock_config):
        """get_reboot_information の XML parse エラー + --force → clear_reboot へ blind fall-through (issue #60)"""
        dev = MagicMock()
        dev.rpc.get_reboot_information.side_effect = etree.XMLSyntaxError(
            "Opening and ending tag mismatch: request-reboot-status line 3 and rpc-reply",
            None, 21, 13,
        )
        # clear_reboot は成功させる
        dev.rpc.request_reboot_clear.return_value = etree.Element("output")
        mock_sw = MagicMock()
        mock_sw.reboot.return_value = "Shutdown at Fri Jun 13 05:00:00 2025. [pid 97978]"
        mock_args.force = True
        reboot_dt = datetime.datetime(2025, 6, 13, 5, 0)
        with patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}):
            with patch.object(junos_upgrade, "clear_reboot", return_value={"ok": True, "message": "cleared"}) as mock_clear:
                with patch("junos_ops.upgrade.SW", return_value=mock_sw):
                    result = junos_upgrade.reboot("test-host", dev, reboot_dt)
        # blind clear が呼ばれて reboot が成功する
        mock_clear.assert_called_once_with(dev, member=None)
        assert result["cleared_existing"] is True
        assert result["code"] == 0
        assert result["ok"] is True
        assert any(
            "clearing reboot schedule blindly" in step.get("message", "")
            for step in result["steps"]
        )

    def test_reboot_xml_parse_error_with_force_but_clear_fails(
        self, junos_upgrade, mock_args, mock_config
    ):
        """parse エラー + --force だが blind clear_reboot も失敗 → code=3 (clear_reboot_failed)"""
        dev = MagicMock()
        dev.rpc.get_reboot_information.side_effect = etree.XMLSyntaxError(
            "Opening and ending tag mismatch: request-reboot-status line 3 and rpc-reply",
            None, 21, 13,
        )
        mock_args.force = True
        reboot_dt = datetime.datetime(2025, 6, 13, 5, 0)
        with patch.object(
            junos_upgrade, "clear_reboot",
            return_value={"ok": False, "message": "clear failed"},
        ) as mock_clear:
            result = junos_upgrade.reboot("test-host", dev, reboot_dt)
        mock_clear.assert_called_once_with(dev, member=None)
        # parse_error path でも従来の clear_reboot_failed は code=3 を維持
        assert result["code"] == 3
        assert result["error"] == "clear_reboot_failed"
        assert result["cleared_existing"] is False


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


class TestRebootMember:
    """reboot(member=N): VC validation, mixed-version gate, raw request-reboot RPC."""

    @staticmethod
    def _reboot_xml(text="No shutdown/reboot scheduled.\n"):
        root = etree.Element("output")
        root.text = text
        return root

    @staticmethod
    def _status(master="0", backup="1", members=None):
        members = members or [
            {"id": "0", "role": "Master", "status": "Prsnt", "priority": "129", "model": "m"},
            {"id": "1", "role": "Backup", "status": "Prsnt", "priority": "129", "model": "m"},
        ]
        return {"ok": True, "mode": "Enabled", "members": members,
                "master": master, "backup": backup, "error": None, "error_message": None}

    def _dev(self):
        dev = MagicMock()
        dev.rpc.get_reboot_information.return_value = self._reboot_xml()
        status_el = etree.Element("request-reboot-status")
        status_el.text = "Shutdown NOW!"
        dev.rpc.request_reboot.return_value = status_el
        return dev

    def _run(self, junos_upgrade, dev, member=1, reboot_dt=None, status=None, pending=None):
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=status or self._status()),
            patch.object(junos_upgrade, "get_pending_version", return_value=pending),
            patch.object(junos_upgrade, "check_and_reinstall",
                         return_value={"ok": True, "steps": []}) as reinstall,
            patch("junos_ops.upgrade.SW") as sw_cls,
        ):
            result = junos_upgrade.reboot("test-host", dev, reboot_dt, member=member)
        return result, reinstall, sw_cls

    def test_member_now_uses_raw_rpc_not_sw(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        result, reinstall, sw_cls = self._run(junos_upgrade, dev, member=1)
        assert result["code"] == 0 and result["ok"] is True
        assert result["reboot_at"] == "now"
        assert result["member"] == 1
        dev.rpc.request_reboot.assert_called_once_with(member="1", **{"in": "0"})
        sw_cls.assert_not_called()
        reinstall.assert_called_once()
        assert result["message"] == "Shutdown NOW!"
        assert any(s["action"] == "vc_member" for s in result["steps"])

    def test_member_at_schedules_via_rpc(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        result, _, _ = self._run(
            junos_upgrade, dev, member=1, reboot_dt=datetime.datetime(2025, 6, 13, 5, 0)
        )
        assert result["reboot_at"] == "2506130500"
        dev.rpc.request_reboot.assert_called_once_with(member="1", at="2506130500")

    def test_dry_run_issues_nothing(self, junos_upgrade, mock_args, mock_config):
        mock_args.dry_run = True
        dev = self._dev()
        result, _, _ = self._run(junos_upgrade, dev, member=1)
        assert result["ok"] is True
        assert "dry-run: reboot member 1 now" in result["message"]
        dev.rpc.request_reboot.assert_not_called()

    def test_member_is_master_refused(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        result, reinstall, _ = self._run(junos_upgrade, dev, member=0)
        assert result["code"] == 8
        assert result["error"] == "member_is_master"
        dev.rpc.request_reboot.assert_not_called()
        dev.rpc.get_reboot_information.assert_not_called()
        reinstall.assert_not_called()

    def test_member_is_master_with_force(self, junos_upgrade, mock_args, mock_config):
        mock_args.force = True
        dev = self._dev()
        result, _, _ = self._run(junos_upgrade, dev, member=0)
        assert result["code"] == 0
        assert any(s["action"] == "force_master" for s in result["steps"])
        dev.rpc.request_reboot.assert_called_once()

    def test_member_not_present(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        members = [
            {"id": "0", "role": "Master", "status": "Prsnt", "priority": None, "model": None},
            {"id": "1", "role": "", "status": "NotPrsnt", "priority": None, "model": None},
        ]
        result, _, _ = self._run(
            junos_upgrade, dev, member=1, status=self._status(backup=None, members=members)
        )
        assert result["code"] == 8
        assert result["error"] == "member_not_present"
        assert "1=?/NotPrsnt" in result["message"]
        dev.rpc.request_reboot.assert_not_called()

    def test_unknown_member_id(self, junos_upgrade, mock_args, mock_config):
        result, _, _ = self._run(junos_upgrade, self._dev(), member=7)
        assert result["error"] == "member_not_present"

    def test_vc_status_unavailable(self, junos_upgrade, mock_args, mock_config):
        bad = {"ok": False, "mode": None, "members": [], "master": None, "backup": None,
               "error": "RpcError", "error_message": "boom"}
        result, _, _ = self._run(junos_upgrade, self._dev(), member=1, status=bad)
        assert result["code"] == 8
        assert result["error"] == "vc_status_unavailable"
        assert result["vc_status"] is bad

    def test_pending_package_refused_before_reinstall(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        result, reinstall, _ = self._run(junos_upgrade, dev, member=1, pending="23.4R2-S3")
        assert result["code"] == 9
        assert result["error"] == "pending_package_mixed_version"
        assert "23.4R2-S3" in result["message"]
        reinstall.assert_not_called()
        dev.rpc.request_reboot.assert_not_called()

    def test_pending_package_allowed_with_flag(self, junos_upgrade, mock_args, mock_config):
        mock_args.allow_mixed_version = True
        dev = self._dev()
        result, reinstall, _ = self._run(junos_upgrade, dev, member=1, pending="23.4R2-S3")
        assert result["code"] == 0
        warn = [s for s in result["steps"] if s["action"] == "mixed_version"]
        assert warn and "23.4R2-S3" in warn[0]["message"]
        reinstall.assert_called_once()
        dev.rpc.request_reboot.assert_called_once()

    def test_rpc_error_is_code_5(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev()
        dev.rpc.request_reboot.side_effect = RpcError(
            rsp=etree.fromstring("<rpc-error><error-message>x</error-message></rpc-error>")
        )
        result, _, _ = self._run(junos_upgrade, dev, member=1)
        assert result["code"] == 5 and result["error"] == "RpcError"

    def test_whole_chassis_path_unchanged(self, junos_upgrade, mock_args, mock_config):
        """member=None keeps using SW.reboot(at=...) and never calls vc."""
        dev = self._dev()
        mock_sw = MagicMock()
        mock_sw.reboot.return_value = "Shutdown at ..."
        with (
            patch("junos_ops.upgrade.vc.get_vc_status") as get_status,
            patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}),
            patch("junos_ops.upgrade.SW", return_value=mock_sw),
        ):
            result = junos_upgrade.reboot("test-host", dev, datetime.datetime(2025, 6, 13, 5, 0))
        assert result["code"] == 0 and result["member"] is None
        get_status.assert_not_called()
        mock_sw.reboot.assert_called_once_with(at="2506130500")
        dev.rpc.request_reboot.assert_not_called()


class TestRebootMemberRpcHelper:
    def test_output_lines_fallback(self, junos_upgrade):
        dev = MagicMock()
        rsp = etree.fromstring("<multi><output> line1 </output><output/><output>line2</output></multi>")
        dev.rpc.request_reboot.return_value = rsp
        assert junos_upgrade._reboot_member(dev, 0, None) == "line1\nline2"

    def test_bool_reply_fallback(self, junos_upgrade):
        dev = MagicMock()
        dev.rpc.request_reboot.return_value = True
        assert junos_upgrade._reboot_member(dev, 2, "2506130500") == "request system reboot member 2 issued"
        dev.rpc.request_reboot.assert_called_once_with(member="2", at="2506130500")


class TestRebootMemberReviewFollowups:
    """Fail-closed pending check, API guard, member-aware schedule text."""

    def test_reboot_dt_none_without_member_is_refused(self, junos_upgrade, mock_args, mock_config):
        dev = MagicMock()
        with patch("junos_ops.upgrade.SW") as sw_cls:
            result = junos_upgrade.reboot("test-host", dev, None)
        assert result["code"] == 1 and result["error"] == "reboot_time_required"
        sw_cls.assert_not_called()
        dev.rpc.get_reboot_information.assert_not_called()

    def test_pending_check_failure_refused(self, junos_upgrade, mock_args, mock_config):
        dev = TestRebootMember()._dev()
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=TestRebootMember._status()),
            patch.object(junos_upgrade, "get_pending_version", side_effect=TimeoutError("rpc")),
            patch.object(junos_upgrade, "check_and_reinstall") as reinstall,
        ):
            result = junos_upgrade.reboot("test-host", dev, None, member=1)
        assert result["code"] == 9 and result["error"] == "pending_unknown"
        reinstall.assert_not_called()
        dev.rpc.request_reboot.assert_not_called()

    def test_pending_check_failure_allowed_with_flag(self, junos_upgrade, mock_args, mock_config):
        mock_args.allow_mixed_version = True
        dev = TestRebootMember()._dev()
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=TestRebootMember._status()),
            patch.object(junos_upgrade, "get_pending_version", side_effect=TimeoutError("rpc")),
            patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}),
        ):
            result = junos_upgrade.reboot("test-host", dev, None, member=1)
        assert result["code"] == 0
        assert any(s["action"] == "mixed_version" and "check failed" in s["message"]
                   for s in result["steps"])

    def test_pending_is_queried_strictly(self, junos_upgrade, mock_args, mock_config):
        dev = TestRebootMember()._dev()
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=TestRebootMember._status()),
            patch.object(junos_upgrade, "get_pending_version", return_value=None) as gpv,
            patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}),
        ):
            junos_upgrade.reboot("test-host", dev, None, member=1)
        gpv.assert_called_once_with("test-host", dev, strict=True)

    def test_member_section_selects_target_block(self, junos_upgrade):
        text = (
            "fpc0:\n----\nNo shutdown/reboot scheduled.\n\n"
            "fpc1:\n----\nreboot requested by admin at Fri Jun 13 05:00:00 2025\n"
        )
        assert "requested" in junos_upgrade._member_section(text, 1)
        assert "requested" not in junos_upgrade._member_section(text, 0)
        assert junos_upgrade._member_section("single RE text", 0) == "single RE text"

    def test_schedule_on_target_member_detected_and_cleared_with_member(
        self, junos_upgrade, mock_args, mock_config
    ):
        mock_args.force = True
        dev = TestRebootMember()._dev()
        info = etree.Element("output")
        info.text = (
            "fpc0:\n----\nNo shutdown/reboot scheduled.\n\n"
            "fpc1:\n----\nreboot requested by admin at Fri Jun 13 05:00:00 2025\n"
        )
        dev.rpc.get_reboot_information.return_value = info
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=TestRebootMember._status()),
            patch.object(junos_upgrade, "get_pending_version", return_value=None),
            patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}),
            patch.object(junos_upgrade, "clear_reboot", return_value={"ok": True, "message": "cleared"}) as clear,
        ):
            result = junos_upgrade.reboot("test-host", dev, None, member=1)
        assert result["existing_schedule"] is not None
        assert result["cleared_existing"] is True
        clear.assert_called_once_with(dev, member=1)
        assert result["code"] == 0

    def test_clear_reboot_member_rpc(self, junos_upgrade, mock_args):
        dev = MagicMock()
        out = etree.Element("output")
        out.text = "Terminating..."
        dev.rpc.clear_reboot.return_value = out
        result = junos_upgrade.clear_reboot(dev, member=1)
        assert result["ok"] is True
        dev.rpc.clear_reboot.assert_called_once_with({"format": "text"}, member="1")


class TestGetPendingVersionStrict:
    def test_strict_reraises_rpc_error(self, junos_upgrade, mock_args, mock_config):
        dev = MagicMock()
        dev.facts = {"personality": "MX"}
        dev.rpc.get_software_information.side_effect = RpcError()
        assert junos_upgrade.get_pending_version("h", dev) is None
        with pytest.raises(RpcError):
            junos_upgrade.get_pending_version("h", dev, strict=True)

    def test_strict_unknown_personality(self, junos_upgrade, mock_args, mock_config):
        dev = MagicMock()
        dev.facts = {"personality": "WEIRD"}
        dev.rpc.get_software_information.return_value = etree.Element("output")
        assert junos_upgrade.get_pending_version("h", dev) is None
        with pytest.raises(LookupError):
            junos_upgrade.get_pending_version("h", dev, strict=True)

    def test_strict_install_log_failure(self, junos_upgrade, mock_args, mock_config):
        dev = MagicMock()
        dev.facts = {"personality": "SWITCH"}
        out = etree.Element("output")
        out.text = "Junos: 20.4R3\n"
        dev.rpc.get_software_information.return_value = out
        dev.rpc.get_log.side_effect = RpcError()
        assert junos_upgrade.get_pending_version("h", dev) is None
        with pytest.raises(RpcError):
            junos_upgrade.get_pending_version("h", dev, strict=True)


class TestPendingEqualsRunning:
    """Install-log 'pending' equal to the running version is only cleared when
    the staging predates the last boot."""

    def _dev(self, running):
        dev = TestRebootMember()._dev()
        dev.facts = {"version": running, "personality": "SWITCH", "model": "qfx5110-48s-4c"}
        return dev

    def _run(self, junos_upgrade, dev, pending, stale):
        with (
            patch("junos_ops.upgrade.vc.get_vc_status", return_value=TestRebootMember._status()),
            patch.object(junos_upgrade, "get_pending_version", return_value=pending),
            patch.object(junos_upgrade, "_install_log_staged_before_boot", return_value=stale) as chk,
            patch.object(junos_upgrade, "check_and_reinstall", return_value={"ok": True, "steps": []}),
        ):
            return junos_upgrade.reboot("test-host", dev, None, member=1), chk

    def test_same_version_staged_before_boot_is_not_pending(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev("23.4R2-S8.7")
        result, chk = self._run(junos_upgrade, dev, "23.4R2-S8.7", stale=True)
        assert result["code"] == 0
        chk.assert_called_once_with("test-host", dev, member=1, master="0")
        assert any(s["action"] == "pending_active" for s in result["steps"])
        dev.rpc.request_reboot.assert_called_once()

    def test_same_version_staged_after_boot_is_pending(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev("23.4R2-S8.7")
        result, _ = self._run(junos_upgrade, dev, "23.4R2-S8.7", stale=False)
        assert result["code"] == 9 and result["error"] == "pending_package_mixed_version"
        assert any(s["action"] == "pending_same_version" and "re-install" in s["message"]
                   for s in result["steps"])
        dev.rpc.request_reboot.assert_not_called()

    def test_same_version_unknown_staging_time_stays_refused(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev("23.4R2-S8.7")
        result, _ = self._run(junos_upgrade, dev, "23.4R2-S8.7", stale=None)
        assert result["code"] == 9
        assert any("could not be verified" in s["message"] for s in result["steps"])

    def test_newer_pending_still_refused_without_log_check(self, junos_upgrade, mock_args, mock_config):
        dev = self._dev("23.4R2-S8.7")
        result, chk = self._run(junos_upgrade, dev, "23.4R2-S9.3", stale=True)
        assert result["code"] == 9
        chk.assert_not_called()

    def test_normalisation_collision_is_not_equal(self, junos_upgrade):
        """compare_version treats 23.4R2-S8.7 and 23.4R2008.7 as equal; the gate must not."""
        dev = MagicMock()
        dev.facts = {"version": "23.4R2008.7"}
        assert junos_upgrade.compare_version("23.4R2-S8.7", "23.4R2008.7") == 0
        assert junos_upgrade._pending_is_running("23.4R2-S8.7", dev) is False

    def test_unknown_running_version(self, junos_upgrade):
        dev = MagicMock()
        dev.facts = {"version": None}
        assert junos_upgrade._pending_is_running("23.4R2-S8.7", dev) is False


class TestInstallLogStagedBeforeBoot:
    LOG = (
        "2026-05-13 14:23:09 JST mgd[67644]: /usr/libexec/ui/package -X rollback\n"
        "&lt;output&gt;\n...\n&lt;/output&gt;\n"
        "2026-06-16 17:33:26 JST mgd[51697]: /usr/libexec/ui/package -X update -no-validate "
        "/var/tmp/jinstall-host-qfx-5e-x86-64-23.4R2-S8.7-secure-signed.tgz\n"
        "&lt;output&gt;\nupgrade_platform: Staging of /var/tmp/jinstall-qfx-5e-junos-23.4R2-S8.7-secure-linux.tgz completed\n"
        "&lt;/output&gt;\n&lt;package-result&gt;0&lt;/package-result&gt;\n"
    )

    def _uptime(self, booted0="2026-06-17 04:11:55 JST", booted1="2026-06-17 04:11:43 JST"):
        return etree.fromstring(
            "<multi-routing-engine-results>"
            f"<multi-routing-engine-item><re-name>localre</re-name><system-uptime-information>"
            f"<system-booted-time><date-time>{booted0}</date-time></system-booted-time>"
            "</system-uptime-information></multi-routing-engine-item>"
            f"<multi-routing-engine-item><re-name>fpc1</re-name><system-uptime-information>"
            f"<system-booted-time><date-time>{booted1}</date-time></system-booted-time>"
            "</system-uptime-information></multi-routing-engine-item>"
            "</multi-routing-engine-results>"
        )

    def _dev(self, log=None, uptime=None):
        dev = MagicMock()
        out = etree.Element("output")
        out.text = log if log is not None else self.LOG.replace("&lt;", "<").replace("&gt;", ">")
        dev.rpc.get_log.return_value = out
        dev.rpc.get_system_uptime_information.return_value = uptime if uptime is not None else self._uptime()
        return dev

    def test_staged_before_boot(self, junos_upgrade):
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(), member=1) is True

    def test_staged_after_boot_uses_member_block(self, junos_upgrade):
        # fpc1 booted before the staging -> genuinely pending on member 1
        dev = self._dev(uptime=self._uptime(booted1="2026-06-01 00:00:00 JST"))
        assert junos_upgrade._install_log_staged_before_boot("h", dev, member=1) is False
        # member 0 (localre block) still after
        assert junos_upgrade._install_log_staged_before_boot("h", dev, member=0, master="0") is True

    def test_master_member_uses_localre_block(self, junos_upgrade):
        dev = self._dev(uptime=self._uptime(booted0="2026-06-01 00:00:00 JST"))
        # member 0 is the master -> its block is "localre"
        assert junos_upgrade._install_log_staged_before_boot("h", dev, member=0, master="0") is False
        # without master info, member 0 has no fpc0 block -> unknown, never another member's time
        assert junos_upgrade._install_log_staged_before_boot("h", dev, member=0) is None

    def test_missing_member_block_is_unknown(self, junos_upgrade):
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(), member=2, master="0") is None

    def test_single_re_uptime(self, junos_upgrade):
        up = etree.fromstring(
            "<system-uptime-information><system-booted-time>"
            "<date-time>2026-06-17 04:11:55 JST</date-time></system-booted-time></system-uptime-information>"
        )
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(uptime=up)) is True

    def test_single_operation_log_first_line_matches(self, junos_upgrade):
        """The only header is the first line of <output>; must still be found."""
        log = (
            "2026-06-16 17:33:26 JST mgd[51697]: /usr/libexec/ui/package -X update /var/tmp/x.tgz\n"
            "<output>\nupgrade_platform: Staging of /var/tmp/x.tgz completed\n</output>\n"
        )
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(log=log), member=1) is True

    def test_no_header_is_unknown(self, junos_upgrade):
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(log="no headers here")) is None

    def test_rpc_failure_is_unknown(self, junos_upgrade):
        dev = self._dev()
        dev.rpc.get_system_uptime_information.side_effect = RpcError()
        assert junos_upgrade._install_log_staged_before_boot("h", dev) is None

    def test_uses_last_header(self, junos_upgrade):
        log = self.LOG.replace("&lt;", "<").replace("&gt;", ">") + (
            "2026-06-20 09:00:00 JST mgd[1]: /usr/libexec/ui/package -X update /var/tmp/x.tgz\n<output>\n</output>\n"
        )
        assert junos_upgrade._install_log_staged_before_boot("h", self._dev(log=log)) is False
