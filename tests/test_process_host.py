"""process_host() の統合テスト"""

import configparser
from unittest.mock import patch, MagicMock, call

from jnpr.junos.exception import ConnectClosedError


class TestProcessHost:
    """process_host() のテスト"""

    def test_connect_fail(self, junos_update, mock_args, mock_config):
        """接続失敗時に 1 を返す"""
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": False, "dev": None, "error": "ConnectError", "error_message": "mock connect error"}):
            result = junos_update.process_host("test-host")
            assert result == 1

    def test_default_action(self, junos_update, mock_args, mock_config):
        """オプションなし → pprint(facts) して 0 を返す"""
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T", "hostname": "test"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = junos_update.process_host("test-host")
            assert result == 0
            mock_dev.close.assert_called_once()

    def test_copy_success(self, junos_update, mock_args, mock_config):
        """--copy 成功時に 0 を返す"""
        junos_update.args.copy = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(junos_update, "copy", return_value=False):
                result = junos_update.process_host("test-host")
                assert result == 0

    def test_copy_failure(self, junos_update, mock_args, mock_config):
        """--copy 失敗時に 1 を返す"""
        junos_update.args.copy = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(junos_update, "copy", return_value=True):
                result = junos_update.process_host("test-host")
                assert result == 1

    def test_exception_caught(self, junos_update, mock_args, mock_config):
        """get_model_file等の例外が try/except でキャッチされ 1 を返す"""
        junos_update.args.copy = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(
                junos_update, "copy",
                side_effect=configparser.NoOptionError("unknown.file", "test-host"),
            ):
                result = junos_update.process_host("test-host")
                assert result == 1

    def test_dev_close_always_called(self, junos_update, mock_args, mock_config):
        """成功時でも dev.close() が呼ばれる"""
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            junos_update.process_host("test-host")
            mock_dev.close.assert_called_once()

    def test_dev_close_on_error(self, junos_update, mock_args, mock_config):
        """エラー時でも dev.close() が呼ばれる"""
        junos_update.args.copy = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(junos_update, "copy", return_value=True):
                junos_update.process_host("test-host")
                mock_dev.close.assert_called_once()

    def test_dev_close_already_closed(self, junos_update, mock_args, mock_config):
        """dev.close() で ConnectClosedError が出ても無視される"""
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        mock_dev.close.side_effect = ConnectClosedError(mock_dev)
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = junos_update.process_host("test-host")
            assert result == 0

    def test_showversion(self, junos_update, mock_args, mock_config):
        """--showversion 成功時に 0 を返す"""
        from junos_ops import display
        junos_update.args.showversion = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        fake_result = {"hostname": "test-host", "model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(junos_update, "show_version", return_value=fake_result):
                with patch.object(display, "print_version"):
                    result = junos_update.process_host("test-host")
                    assert result == 0

    def test_install_success(self, junos_update, mock_args, mock_config):
        """--install 成功時に 0 を返す"""
        junos_update.args.install = True
        mock_dev = MagicMock()
        mock_dev.facts = {"model": "EX2300-24T"}
        with patch.object(junos_update, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch.object(junos_update, "install", return_value=False):
                result = junos_update.process_host("test-host")
                assert result == 0
