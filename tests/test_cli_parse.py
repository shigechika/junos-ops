"""CLI 引数パースのテスト（Issue #36: サブコマンドなしで -c 指定時のエラー修正）"""

import sys
from unittest.mock import patch, MagicMock

import pytest

from junos_ops import cli


class TestNoSubcommandParsing:
    """サブコマンドなしの引数パースが正しく動作するかテスト"""

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    @patch("junos_ops.common.get_default_config", return_value="config.ini")
    def test_config_only(self, mock_default, mock_read, mock_targets, mock_run):
        """-c のみ指定時にエラーにならない（Issue #36）"""
        with patch.object(sys, "argv", ["junos-ops", "-c", "accounts.ini"]):
            cli.main()
        # facts (subcommand=None) として実行される
        mock_run.assert_called_once()
        func = mock_run.call_args[0][0]
        assert func == cli.cmd_facts

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    @patch("junos_ops.common.get_default_config", return_value="config.ini")
    def test_config_with_hostname(self, mock_default, mock_read, mock_targets, mock_run):
        """-c とホスト名を指定"""
        with patch.object(sys, "argv", ["junos-ops", "-c", "accounts.ini", "host1"]):
            cli.main()
        mock_run.assert_called_once()
        func = mock_run.call_args[0][0]
        assert func == cli.cmd_facts

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    @patch("junos_ops.common.get_default_config", return_value="config.ini")
    def test_hostname_only(self, mock_default, mock_read, mock_targets, mock_run):
        """ホスト名のみ指定で facts として実行"""
        with patch.object(sys, "argv", ["junos-ops", "hostname1"]):
            cli.main()
        mock_run.assert_called_once()
        func = mock_run.call_args[0][0]
        assert func == cli.cmd_facts

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    @patch("junos_ops.common.get_default_config", return_value="config.ini")
    def test_config_with_dry_run(self, mock_default, mock_read, mock_targets, mock_run):
        """-c -n で facts dry-run"""
        with patch.object(sys, "argv", ["junos-ops", "-c", "accounts.ini", "-n"]):
            cli.main()
        assert cli.common.args.dry_run is True
        assert cli.common.args.config == "accounts.ini"

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    def test_subcommand_still_works(self, mock_read, mock_targets, mock_run):
        """サブコマンド指定時は従来通り動作"""
        with patch.object(sys, "argv", ["junos-ops", "version", "-c", "config.ini", "host1"]):
            cli.main()
        mock_run.assert_called_once()
        func = mock_run.call_args[0][0]
        assert func == cli.cmd_version

    def test_no_args_shows_help(self, capsys):
        """引数なしでヘルプ表示"""
        with patch.object(sys, "argv", ["junos-ops"]):
            ret = cli.main()
        assert ret == 0
        captured = capsys.readouterr()
        assert "junos-ops" in captured.out

    def test_version_flag(self):
        """--version で正常終了"""
        with patch.object(sys, "argv", ["junos-ops", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 0


class TestUnlinkOption:
    """--unlink オプションのパーステスト (Phase 1: low-flash device support)"""

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    def test_upgrade_unlink(self, mock_read, mock_targets, mock_run):
        """upgrade --unlink で args.unlink=True"""
        with patch.object(sys, "argv", ["junos-ops", "upgrade", "--unlink", "-c", "config.ini", "host1"]):
            cli.main()
        assert cli.common.args.unlink is True
        assert cli.common.args.subcommand == "upgrade"

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    def test_install_unlink(self, mock_read, mock_targets, mock_run):
        """install --unlink で args.unlink=True"""
        with patch.object(sys, "argv", ["junos-ops", "install", "--unlink", "-c", "config.ini", "host1"]):
            cli.main()
        assert cli.common.args.unlink is True
        assert cli.common.args.subcommand == "install"

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["test-host"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["test-host"], "error": None})
    def test_upgrade_without_unlink_defaults_false(self, mock_read, mock_targets, mock_run):
        """upgrade のみ指定時は args.unlink=False（デフォルト）"""
        with patch.object(sys, "argv", ["junos-ops", "upgrade", "-c", "config.ini", "host1"]):
            cli.main()
        assert cli.common.args.unlink is False
