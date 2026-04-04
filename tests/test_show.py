"""show サブコマンドのテスト"""

from unittest.mock import MagicMock, patch, call
from jnpr.junos.exception import RpcTimeoutError

from junos_ops import cli
from junos_ops import common


class TestCmdShow:
    """cmd_show() のテスト"""

    def test_connect_fail(self, junos_common, mock_args, mock_config):
        """接続失敗時に 1 を返す"""
        mock_args.show_command = "show version"
        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": False, "dev": None, "error": "ConnectError", "error_message": "mock connect error"}):
            result = cli.cmd_show("test-host")
            assert result == 1

    def test_success(self, junos_common, mock_args, mock_config, capsys):
        """正常時にホスト名付きで出力される"""
        mock_args.show_command = "show version"
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "  Hostname: test-host\nModel: MX204  \n"

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 0
        mock_dev.cli.assert_called_once_with("show version")
        mock_dev.close.assert_called_once()
        captured = capsys.readouterr()
        assert "# test-host" in captured.out
        assert "Hostname: test-host" in captured.out
        assert "Model: MX204" in captured.out

    def test_exception(self, junos_common, mock_args, mock_config):
        """dev.cli() 例外時に 1 を返す"""
        mock_args.show_command = "show bgp summary"
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = Exception("RPC timeout")

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 1
        mock_dev.close.assert_called_once()

    def test_dev_close_on_exception(self, junos_common, mock_args, mock_config):
        """例外時でも dev.close() が呼ばれる"""
        mock_args.show_command = "show interfaces terse"
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = RuntimeError("unexpected")

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 1
        mock_dev.close.assert_called_once()

    def test_close_exception_suppressed(self, junos_common, mock_args, mock_config):
        """dev.close() の例外が握り潰される"""
        mock_args.show_command = "show version"
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "output"
        mock_dev.close.side_effect = Exception("close failed")

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 0

    def test_show_command_passed_to_cli(self, junos_common, mock_args, mock_config):
        """args.show_command がそのまま dev.cli() に渡される"""
        mock_args.show_command = "show configuration system login user nttview"
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "user nttview { ... }"

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 0
        mock_dev.cli.assert_called_once_with(
            "show configuration system login user nttview"
        )


class TestCmdShowFile:
    """cmd_show() の -f ファイルモードのテスト"""

    def test_showfile_success(self, junos_common, mock_args, mock_config, capsys):
        """複数コマンドファイルからの正常実行、出力フォーマット確認"""
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = [
            "  terse output  ",
            "  route summary  ",
        ]

        with (
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
            patch.object(
                cli.common, "load_commands",
                return_value=["show interfaces terse", "show route summary"],
            ),
        ):
            result = cli.cmd_show("test-host")

        assert result == 0
        assert mock_dev.cli.call_count == 2
        mock_dev.cli.assert_any_call("show interfaces terse")
        mock_dev.cli.assert_any_call("show route summary")
        mock_dev.close.assert_called_once()

        captured = capsys.readouterr()
        assert "# test-host" in captured.out
        assert "## show interfaces terse" in captured.out
        assert "terse output" in captured.out
        assert "## show route summary" in captured.out
        assert "route summary" in captured.out

    def test_showfile_skips_comments_and_blanks(
        self, junos_common, mock_args, mock_config, capsys
    ):
        """load_commands がコメント行と空行をスキップすることの確認"""
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "output"

        with (
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
            patch.object(
                cli.common, "load_commands",
                return_value=["show version"],
            ),
        ):
            result = cli.cmd_show("test-host")

        assert result == 0
        # load_commands がフィルタ済みの1コマンドだけ返すので cli() は1回
        mock_dev.cli.assert_called_once_with("show version")

    def test_showfile_exception_on_one_command(
        self, junos_common, mock_args, mock_config
    ):
        """途中のコマンドで例外 → エラー返却"""
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = Exception("RPC timeout")

        with (
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
            patch.object(
                cli.common, "load_commands",
                return_value=["show version", "show bgp summary"],
            ),
        ):
            result = cli.cmd_show("test-host")

        assert result == 1
        mock_dev.close.assert_called_once()

    def test_showfile_connect_fail(self, junos_common, mock_args, mock_config):
        """接続失敗時に 1 を返す"""
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": False, "dev": None, "error": "ConnectError", "error_message": "mock connect error"}):
            result = cli.cmd_show("test-host")
            assert result == 1


class TestLoadCommands:
    """common.load_commands() ヘルパーの単体テスト"""

    def test_load_commands(self, tmp_path):
        """コメント行と空行を除外してコマンド行のみ返す"""
        cmd_file = tmp_path / "commands.txt"
        cmd_file.write_text(
            "# コメント行\n"
            "show version\n"
            "\n"
            "  # インデント付きコメント\n"
            "show interfaces terse\n"
            "  show route summary  \n"
        )
        result = common.load_commands(str(cmd_file))
        assert result == [
            "show version",
            "show interfaces terse",
            "show route summary",
        ]

    def test_load_commands_empty_file(self, tmp_path):
        """空ファイルからは空リストが返る"""
        cmd_file = tmp_path / "empty.txt"
        cmd_file.write_text("")
        result = common.load_commands(str(cmd_file))
        assert result == []

    def test_load_commands_only_comments(self, tmp_path):
        """コメントのみのファイルからは空リストが返る"""
        cmd_file = tmp_path / "comments.txt"
        cmd_file.write_text("# comment 1\n# comment 2\n\n")
        result = common.load_commands(str(cmd_file))
        assert result == []


class TestCliWithRetry:
    """_cli_with_retry() のテスト (Issue #38)"""

    @patch("junos_ops.cli.time.sleep")
    def test_retry_success_after_timeout(self, mock_sleep, junos_common, mock_args):
        """1回目 RpcTimeoutError → 2回目成功"""
        mock_args.retry = 2
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = [RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30), "output"]
        result = cli._cli_with_retry(mock_dev, "show version", "test-host", 2)
        assert result == "output"
        assert mock_dev.cli.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("junos_ops.cli.time.sleep")
    def test_retry_exhausted(self, mock_sleep, junos_common, mock_args):
        """リトライ回数を使い切ったら RpcTimeoutError を再送出"""
        import pytest
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30)
        with pytest.raises(RpcTimeoutError):
            cli._cli_with_retry(mock_dev, "show version", "test-host", 2)
        assert mock_dev.cli.call_count == 3  # 初回 + リトライ2回
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(5), call(10)])

    def test_no_retry(self, junos_common, mock_args):
        """retry=0 で RpcTimeoutError はそのまま送出"""
        import pytest
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30)
        with pytest.raises(RpcTimeoutError):
            cli._cli_with_retry(mock_dev, "show version", "test-host", 0)
        assert mock_dev.cli.call_count == 1

    @patch("junos_ops.cli.time.sleep")
    def test_non_timeout_not_retried(self, mock_sleep, junos_common, mock_args):
        """RpcTimeoutError 以外の例外はリトライしない"""
        import pytest
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = RuntimeError("other error")
        with pytest.raises(RuntimeError):
            cli._cli_with_retry(mock_dev, "show version", "test-host", 2)
        assert mock_dev.cli.call_count == 1
        mock_sleep.assert_not_called()

    @patch("junos_ops.cli.time.sleep")
    def test_backoff_increases(self, mock_sleep, junos_common, mock_args):
        """バックオフが 5, 10, 15... と増加する"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = [
            RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30),
            RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30),
            "output",
        ]
        result = cli._cli_with_retry(mock_dev, "show version", "test-host", 3)
        assert result == "output"
        mock_sleep.assert_has_calls([call(5), call(10)])


class TestCmdShowRetry:
    """cmd_show() のリトライ統合テスト (Issue #38)"""

    @patch("junos_ops.cli.time.sleep")
    def test_show_retry_success(self, mock_sleep, junos_common, mock_args,
                                 mock_config, capsys):
        """cmd_show で RpcTimeoutError 後にリトライ成功"""
        mock_args.show_command = "show system alarms"
        mock_args.retry = 1
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = [
            RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30),
            "No alarms currently active",
        ]

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 0
        assert mock_dev.cli.call_count == 2
        captured = capsys.readouterr()
        assert "No alarms currently active" in captured.out

    @patch("junos_ops.cli.time.sleep")
    def test_show_retry_exhausted_returns_error(self, mock_sleep, junos_common,
                                                 mock_args, mock_config):
        """リトライ使い切りで exit code 1"""
        mock_args.show_command = "show system alarms"
        mock_args.retry = 1
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30)

        with patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = cli.cmd_show("test-host")

        assert result == 1
        assert mock_dev.cli.call_count == 2
        mock_dev.close.assert_called_once()

    @patch("junos_ops.cli.time.sleep")
    def test_showfile_retry(self, mock_sleep, junos_common, mock_args,
                             mock_config, capsys):
        """-f モードでもリトライが効く"""
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_args.retry = 1
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = [
            RpcTimeoutError(MagicMock(hostname="test-host"), "command", 30),
            "terse output",
            "route summary",
        ]

        with (
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
            patch.object(
                cli.common, "load_commands",
                return_value=["show interfaces terse", "show route summary"],
            ),
        ):
            result = cli.cmd_show("test-host")

        assert result == 0
        assert mock_dev.cli.call_count == 3
        captured = capsys.readouterr()
        assert "terse output" in captured.out
        assert "route summary" in captured.out
