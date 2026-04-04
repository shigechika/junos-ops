"""connect() のモックテスト（dict 返却）"""

from unittest.mock import patch, MagicMock

from jnpr.junos.exception import (
    ConnectAuthError,
    ConnectRefusedError,
    ConnectTimeoutError,
    ConnectUnknownHostError,
)


class TestConnect:
    """connect() は dict を返す"""

    def test_success(self, junos_common, mock_args, mock_config, capsys):
        """正常接続"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev

            result = junos_common.connect("test-host")

            assert result["ok"] is True
            assert result["dev"] is mock_dev
            assert result["hostname"] == "test-host"
            assert result["host"] == "192.0.2.1"
            assert result["error"] is None
            assert result["error_message"] is None
            mock_dev.open.assert_called_once()
            # core は print しない
            captured = capsys.readouterr()
            assert captured.out == ""

    def _assert_error(self, result, exc_name):
        assert result["ok"] is False
        assert result["dev"] is None
        assert result["error"] == exc_name
        assert result["error_message"]  # non-empty

    def test_auth_error(self, junos_common, mock_args, mock_config, capsys):
        """認証エラー"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev
            mock_dev.open.side_effect = ConnectAuthError(mock_dev)

            result = junos_common.connect("test-host")

            self._assert_error(result, "ConnectAuthError")
            assert "Authentication" in result["error_message"]
            # core は print しない
            assert capsys.readouterr().out == ""

    def test_timeout_error(self, junos_common, mock_args, mock_config, capsys):
        """接続タイムアウト"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev
            mock_dev.open.side_effect = ConnectTimeoutError(mock_dev)

            result = junos_common.connect("test-host")

            self._assert_error(result, "ConnectTimeoutError")
            assert "timeout" in result["error_message"].lower()
            assert capsys.readouterr().out == ""

    def test_refused_error(self, junos_common, mock_args, mock_config, capsys):
        """接続拒否"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev
            mock_dev.open.side_effect = ConnectRefusedError(mock_dev)

            result = junos_common.connect("test-host")

            self._assert_error(result, "ConnectRefusedError")
            assert "refused" in result["error_message"].lower()
            assert capsys.readouterr().out == ""

    def test_unknown_host_error(self, junos_common, mock_args, mock_config, capsys):
        """不明なホスト"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev
            mock_dev.open.side_effect = ConnectUnknownHostError(mock_dev)

            result = junos_common.connect("test-host")

            self._assert_error(result, "ConnectUnknownHostError")
            assert capsys.readouterr().out == ""

    def test_generic_exception(self, junos_common, mock_args, mock_config, capsys):
        """その他の例外"""
        with patch.object(junos_common, "Device") as MockDevice:
            mock_dev = MagicMock()
            MockDevice.return_value = mock_dev
            mock_dev.open.side_effect = Exception("unexpected error")

            result = junos_common.connect("test-host")

            self._assert_error(result, "Exception")
            assert "unexpected error" in result["error_message"]
            assert capsys.readouterr().out == ""
