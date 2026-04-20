"""connect() のモックテスト（dict 返却）"""

import os
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

    def test_ssh_config_passthrough(self, junos_common, mock_args, mock_config, capsys):
        """ssh_config is expanded with ~ and forwarded alongside existing kwargs."""
        mock_config.set("test-host", "ssh_config", "~/.ssh/config")
        with patch.object(junos_common, "Device") as MockDevice:
            MockDevice.return_value = MagicMock()

            junos_common.connect("test-host")

            kw = MockDevice.call_args.kwargs
            assert kw["ssh_config"] == os.path.expanduser("~/.ssh/config")
            # Existing kwargs must survive the refactor to a kwargs dict.
            assert kw["host"] == "192.0.2.1"
            assert kw["port"] == 830
            assert kw["user"] == "testuser"
            assert capsys.readouterr().out == ""

    def test_ssh_config_absent(self, junos_common, mock_args, mock_config, capsys):
        """Without ssh_config set, no kwarg is passed so PyEZ's default applies."""
        with patch.object(junos_common, "Device") as MockDevice:
            MockDevice.return_value = MagicMock()

            junos_common.connect("test-host")

            assert "ssh_config" not in MockDevice.call_args.kwargs
            assert capsys.readouterr().out == ""

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
