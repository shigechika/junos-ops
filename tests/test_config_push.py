"""load_config() のテスト"""

from unittest.mock import MagicMock, patch, call

from junos_ops import cli
from junos_ops import common


class TestLoadConfig:
    """load_config() のテスト"""

    def test_success(self, junos_upgrade, mock_args, mock_config):
        """正常系: load → diff → commit_check → commit confirmed → health check → confirm"""
        dev = MagicMock()
        dev.cli.return_value = (
            "PING 255.255.255.255 (255.255.255.255): 56 data bytes\n"
            "...3 packets transmitted, 3 packets received, 0% packet loss"
        )
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(
                common, "load_commands",
                return_value=["set system host-name test"],
            ),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        mock_cu.lock.assert_called_once()
        mock_cu.load.assert_called_once_with(
            "set system host-name test", format="set",
        )
        mock_cu.diff.assert_called_once()
        mock_cu.pdiff.assert_called_once()
        mock_cu.commit_check.assert_called_once()
        # commit confirmed 1 → health check → commit で確定
        assert mock_cu.commit.call_count == 2
        mock_cu.commit.assert_any_call(confirm=1)
        mock_cu.commit.assert_any_call()
        dev.cli.assert_called_once_with("ping count 3 255.255.255.255 rapid")
        mock_cu.unlock.assert_called_once()

    def test_no_changes(self, junos_upgrade, mock_args, mock_config):
        """差分なし → "no changes" で正常終了"""
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = None
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        mock_cu.lock.assert_called_once()
        mock_cu.load.assert_called_once()
        mock_cu.commit.assert_not_called()
        mock_cu.unlock.assert_called_once()

    def test_dry_run(self, junos_upgrade, mock_args, mock_config):
        """dry-run: diff 表示のみ、commit しない"""
        mock_args.dry_run = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        mock_cu.pdiff.assert_called_once()
        mock_cu.commit.assert_not_called()
        mock_cu.rollback.assert_called_once()
        mock_cu.unlock.assert_called_once()

    def test_commit_check_fail(self, junos_upgrade, mock_args, mock_config):
        """commit_check 失敗 → rollback + unlock"""
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        mock_cu.commit_check.side_effect = Exception("commit check failed")
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.rollback.assert_called_once()
        mock_cu.unlock.assert_called_once()
        mock_cu.commit.assert_not_called()

    def test_commit_fail(self, junos_upgrade, mock_args, mock_config):
        """commit 失敗 → rollback + unlock"""
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        mock_cu.commit.side_effect = Exception("commit failed")
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.rollback.assert_called_once()
        mock_cu.unlock.assert_called_once()

    def test_load_error(self, junos_upgrade, mock_args, mock_config):
        """ファイル読み込みエラー → rollback + unlock"""
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.load.side_effect = Exception("file not found")
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.rollback.assert_called_once()
        mock_cu.unlock.assert_called_once()
        mock_cu.commit.assert_not_called()

    def test_lock_error(self, junos_upgrade, mock_args, mock_config):
        """ロック取得失敗"""
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.lock.side_effect = Exception("lock failed")
        with patch("junos_ops.upgrade.Config", return_value=mock_cu):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.load.assert_not_called()
        mock_cu.commit.assert_not_called()

    def test_custom_confirm_timeout(self, junos_upgrade, mock_args, mock_config):
        """confirm_timeout カスタム値"""
        mock_args.confirm_timeout = 3
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        mock_cu.commit.assert_any_call(confirm=3)


class TestConfigCommentStripping:
    """config -f のコメント行・空行除去テスト"""

    def test_config_comments_stripped(self, junos_upgrade, mock_args, mock_config):
        """# コメント行が除去されて cu.load() に文字列で渡される"""
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(
                common, "load_commands",
                return_value=[
                    "set system host-name test",
                    "set system ntp server 10.0.0.1",
                ],
            ) as mock_load_cmds,
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")

        assert result is False
        # load_commands がファイルパスで呼ばれている
        mock_load_cmds.assert_called_once_with("commands.set")
        # cu.load() に文字列が渡されている（path= ではない）
        mock_cu.load.assert_called_once_with(
            "set system host-name test\nset system ntp server 10.0.0.1",
            format="set",
        )

    def test_config_blank_lines_stripped(self, junos_upgrade, mock_args, mock_config):
        """空行が除去される（load_commands の責務だが統合確認）"""
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(
                common, "load_commands",
                return_value=["set system host-name test"],
            ),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")

        assert result is False
        mock_cu.load.assert_called_once_with(
            "set system host-name test", format="set",
        )


class TestHealthCheck:
    """ヘルスチェック機能のテスト"""

    def test_health_check_ping_success(self, junos_upgrade, mock_args, mock_config):
        """ping 成功 → 最終 commit 実行"""
        dev = MagicMock()
        dev.cli.return_value = (
            "PING 255.255.255.255 (255.255.255.255): 56 data bytes\n"
            "...3 packets transmitted, 3 packets received, 0% packet loss"
        )
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        dev.cli.assert_called_once_with("ping count 3 255.255.255.255 rapid")
        assert mock_cu.commit.call_count == 2

    def test_health_check_ping_fail(self, junos_upgrade, mock_args, mock_config):
        """0 packets received → 最終 commit なし、return True"""
        dev = MagicMock()
        dev.cli.return_value = (
            "PING 255.255.255.255 (255.255.255.255): 56 data bytes\n"
            "...3 packets transmitted, 0 packets received, 100% packet loss"
        )
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        # commit confirmed のみ（最終 commit なし）
        mock_cu.commit.assert_called_once_with(confirm=1)
        mock_cu.unlock.assert_called_once()

    def test_health_check_exception(self, junos_upgrade, mock_args, mock_config):
        """dev.cli() で例外 → 最終 commit なし、return True"""
        dev = MagicMock()
        dev.cli.side_effect = Exception("RPC timeout")
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.commit.assert_called_once_with(confirm=1)
        mock_cu.unlock.assert_called_once()

    def test_health_check_disabled(self, junos_upgrade, mock_args, mock_config):
        """--no-health-check → ヘルスチェックスキップ、commit 2回"""
        mock_args.no_health_check = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        dev.cli.assert_not_called()
        assert mock_cu.commit.call_count == 2

    def test_health_check_custom_command(self, junos_upgrade, mock_args, mock_config):
        """非 ping コマンドが例外なく実行 → 成功"""
        mock_args.health_check = ["show chassis routing-engine"]
        dev = MagicMock()
        dev.cli.return_value = "Routing Engine status: OK"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        dev.cli.assert_called_once_with("show chassis routing-engine")
        assert mock_cu.commit.call_count == 2

    def test_health_check_default(self, mock_args):
        """デフォルト値がリスト ["ping count 3 255.255.255.255 rapid"] であること"""
        assert mock_args.health_check == ["ping count 3 255.255.255.255 rapid"]

    def test_health_check_fallback(self, junos_upgrade, mock_args, mock_config):
        """1つ目失敗 → 2つ目成功でフォールバック、最終 commit 実行"""
        mock_args.health_check = [
            "ping count 3 255.255.255.255 rapid",
            "ping count 3 ::1 rapid",
        ]
        dev = MagicMock()
        dev.cli.side_effect = [
            # 1つ目: 失敗
            "PING 255.255.255.255 (255.255.255.255): 56 data bytes\n"
            "...3 packets transmitted, 0 packets received, 100% packet loss",
            # 2つ目: 成功
            "PING6(56=40+8+8 bytes) ::1 --> ::1\n"
            "...3 packets transmitted, 3 packets received, 0% packet loss",
        ]
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        assert dev.cli.call_count == 2
        assert mock_cu.commit.call_count == 2

    def test_health_check_all_fail(self, junos_upgrade, mock_args, mock_config):
        """全コマンド失敗 → 最終 commit なし、return True"""
        mock_args.health_check = [
            "ping count 3 255.255.255.255 rapid",
            "ping count 3 ::1 rapid",
        ]
        dev = MagicMock()
        dev.cli.side_effect = [
            "...3 packets transmitted, 0 packets received, 100% packet loss",
            "...3 packets transmitted, 0 packets received, 100% packet loss",
        ]
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        assert dev.cli.call_count == 2
        mock_cu.commit.assert_called_once_with(confirm=1)
        mock_cu.unlock.assert_called_once()


class TestRpcTimeout:
    """RPC タイムアウトオプションのテスト (Issue #39)"""

    def test_cli_timeout_sets_dev_timeout(self, junos_common, mock_args, mock_config):
        """--timeout 60 で dev.timeout が 60 に設定される"""
        mock_args.rpc_timeout = 60
        mock_args.configfile = "commands.set"
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch.object(cli.common, "connect", return_value=(False, mock_dev)),
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = cli.cmd_config("test-host")
        assert result == 0
        assert mock_dev.timeout == 60

    def test_config_ini_timeout(self, junos_common, mock_args, mock_config):
        """config.ini の timeout 設定で dev.timeout が設定される"""
        mock_args.rpc_timeout = None
        mock_args.configfile = "commands.set"
        mock_config.set("DEFAULT", "timeout", "90")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch.object(cli.common, "connect", return_value=(False, mock_dev)),
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = cli.cmd_config("test-host")
        assert result == 0
        assert mock_dev.timeout == 90

    def test_cli_timeout_overrides_config_ini(self, junos_common, mock_args, mock_config):
        """CLI --timeout が config.ini の timeout より優先される"""
        mock_args.rpc_timeout = 120
        mock_args.configfile = "commands.set"
        mock_config.set("DEFAULT", "timeout", "90")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch.object(cli.common, "connect", return_value=(False, mock_dev)),
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = cli.cmd_config("test-host")
        assert result == 0
        assert mock_dev.timeout == 120

    def test_no_timeout_uses_default_120(self, junos_common, mock_args, mock_config):
        """--timeout 未指定 + config.ini にもなし → dev.timeout = 120（config デフォルト）"""
        mock_args.rpc_timeout = None
        mock_args.configfile = "commands.set"
        mock_dev = MagicMock(spec=["cli", "close", "timeout"])
        mock_dev.timeout = 30  # PyEZ デフォルト
        mock_dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch.object(cli.common, "connect", return_value=(False, mock_dev)),
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = cli.cmd_config("test-host")
        assert result == 0
        assert mock_dev.timeout == 120


class TestNoConfirm:
    """--no-confirm オプションのテスト (Issue #39)"""

    def test_no_confirm_direct_commit(self, junos_upgrade, mock_args, mock_config):
        """--no-confirm で commit confirmed をスキップし直接 commit"""
        mock_args.no_confirm = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        # commit は1回だけ（confirm= なし）
        mock_cu.commit.assert_called_once_with()
        # ヘルスチェックは実行されない
        dev.cli.assert_not_called()

    def test_no_confirm_output(self, junos_upgrade, mock_args, mock_config, capsys):
        """--no-confirm で "commit applied (no confirm)" と表示"""
        mock_args.no_confirm = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            junos_upgrade.load_config("test-host", dev, "commands.set")
        captured = capsys.readouterr()
        assert "commit applied (no confirm)" in captured.out

    def test_no_confirm_commit_error(self, junos_upgrade, mock_args, mock_config):
        """--no-confirm で commit 失敗時も rollback + unlock"""
        mock_args.no_confirm = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        mock_cu.commit.side_effect = Exception("commit failed")
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is True
        mock_cu.rollback.assert_called_once()
        mock_cu.unlock.assert_called_once()

    def test_confirm_default_still_works(self, junos_upgrade, mock_args, mock_config):
        """no_confirm=False（デフォルト）では従来通り commit confirmed フロー"""
        mock_args.no_confirm = False
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result is False
        # commit confirmed + commit の2回
        assert mock_cu.commit.call_count == 2
        mock_cu.commit.assert_any_call(confirm=1)
