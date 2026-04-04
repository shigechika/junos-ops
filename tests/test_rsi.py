"""RSI/SCF 収集のテスト"""

from unittest.mock import patch, MagicMock, mock_open
from lxml import etree

from junos_ops import rsi


class TestGetSupportInformation:
    """get_support_information() のテスト"""

    def test_default_timeout(self):
        """通常機種は timeout=600"""
        dev = MagicMock()
        dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-mx",
            "srx_cluster": None,
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=600
        )

    def test_srx_branch_timeout(self):
        """SRX_BRANCH は timeout=1200"""
        dev = MagicMock()
        dev.facts = {
            "personality": "SRX_BRANCH",
            "model": "SRX345",
            "model_info": {"SRX345": {}},
            "hostname": "test-srx",
            "srx_cluster": None,
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=1200
        )

    def test_ex2300_timeout(self):
        """EX2300-24T は timeout=1200"""
        dev = MagicMock()
        dev.facts = {
            "personality": "SWITCH",
            "model": "EX2300-24T",
            "model_info": {"EX2300-24T": {}},
            "hostname": "test-ex",
            "srx_cluster": None,
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=1200
        )

    def test_virtual_chassis_timeout(self):
        """Virtual Chassis (model_info >= 2) は timeout=1800"""
        dev = MagicMock()
        dev.facts = {
            "personality": "SWITCH",
            "model": "EX4300-48T",
            "model_info": {"EX4300-48T": {}, "EX4300-48T-2": {}},
            "hostname": "test-vc",
            "srx_cluster": None,
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=1800
        )

    def test_qfx5110_timeout(self):
        """QFX5110-48S-4C は timeout=2400"""
        dev = MagicMock()
        dev.facts = {
            "personality": "SWITCH",
            "model": "QFX5110-48S-4C",
            "model_info": {"QFX5110-48S-4C": {}, "QFX5110-48S-4C-2": {}},
            "hostname": "test-qfx",
            "srx_cluster": None,
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=2400
        )

    def test_srx_cluster_node_primary(self):
        """SRX cluster の場合は node='primary' を指定"""
        dev = MagicMock()
        dev.facts = {
            "personality": "SRX_HIGHEND",
            "model": "SRX4600",
            "model_info": {"SRX4600": {}},
            "hostname": "test-cluster",
            "srx_cluster": "True",
        }
        rsi.get_support_information(dev)
        dev.rpc.get_support_information.assert_called_once_with(
            {"format": "text"}, dev_timeout=600, node="primary"
        )

    def test_exception_returns_error_dict(self):
        """例外発生時は ok=False の dict を返す"""
        dev = MagicMock()
        dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-err",
            "srx_cluster": None,
        }
        dev.rpc.get_support_information.side_effect = Exception("RPC failed")
        result = rsi.get_support_information(dev)
        assert result["ok"] is False
        assert result["rpc"] is None
        assert result["error"] == "Exception"
        assert "RPC failed" in result["error_message"]


class TestCmdRsi:
    """cmd_rsi() のテスト"""

    def test_connect_fail(self, junos_common, mock_args, mock_config):
        """接続失敗時に 1 を返す"""
        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": False, "dev": None, "error": "ConnectError", "error_message": "mock connect error"}):
            result = rsi.cmd_rsi("test-host")
            assert result == 1

    def test_success(self, junos_common, mock_args, mock_config, tmp_path):
        """正常時にSCFとRSIファイルが書き出される"""
        mock_config.set("test-host", "RSI_DIR", str(tmp_path) + "/")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "  set system host-name test  \n"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "  RSI output text  \n"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 0
        scf = (tmp_path / "test-host.SCF").read_text()
        assert scf == "set system host-name test"
        rsi_content = (tmp_path / "test-host.RSI").read_text()
        assert rsi_content == "RSI output text"
        mock_dev.close.assert_called_once()

    def test_rsi_failure(self, junos_common, mock_args, mock_config, tmp_path):
        """get_support_information 失敗時に 2 を返す"""
        mock_config.set("test-host", "RSI_DIR", str(tmp_path) + "/")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "config output"
        mock_dev.rpc.get_support_information.side_effect = Exception("timeout")
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 2
        mock_dev.close.assert_called_once()

    def test_dev_close_on_exception(self, junos_common, mock_args, mock_config):
        """例外時でも dev.close() が呼ばれる"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = Exception("unexpected")
        mock_dev.facts = {}

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 1
        mock_dev.close.assert_called_once()

    def test_custom_display_style(self, junos_common, mock_args, mock_config, tmp_path):
        """DISPLAY_STYLE 設定でカスタムコマンドが使われる"""
        mock_config.set("test-host", "RSI_DIR", str(tmp_path) + "/")
        mock_config.set("test-host", "DISPLAY_STYLE",
                        "display set | display omit")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "  set system host-name test  \n"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "RSI text"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 0
        mock_dev.cli.assert_called_once_with(
            "show configuration | display set | display omit"
        )

    def test_default_display_style(self, junos_common, mock_args, mock_config, tmp_path):
        """DISPLAY_STYLE 未設定時はデフォルトの display set が使われる"""
        mock_config.set("test-host", "RSI_DIR", str(tmp_path) + "/")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "config output"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "RSI text"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 0
        mock_dev.cli.assert_called_once_with(
            "show configuration | display set"
        )

    def test_empty_display_style(self, junos_common, mock_args, mock_config, tmp_path):
        """DISPLAY_STYLE が空の場合は stanza 形式（show configuration のみ）"""
        mock_config.set("test-host", "RSI_DIR", str(tmp_path) + "/")
        mock_config.set("test-host", "DISPLAY_STYLE", "")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "system { host-name test; }"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "RSI text"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            result = rsi.cmd_rsi("test-host")

        assert result == 0
        mock_dev.cli.assert_called_once_with("show configuration")

    def test_default_rsi_dir(self, junos_common, mock_args, mock_config):
        """RSI_DIR 未設定時は ./ がデフォルト"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "config"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "RSI text"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        m = mock_open()
        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch("builtins.open", m):
                result = rsi.cmd_rsi("test-host")

        assert result == 0
        # デフォルト ./ が使われている
        m.assert_any_call("./test-host.SCF", mode="w")
        m.assert_any_call("./test-host.RSI", mode="w")

    def test_rsi_dir_tilde_expansion(self, junos_common, mock_args, mock_config):
        """RSI_DIR の ~ がホームディレクトリに展開される"""
        import os
        mock_config.set("test-host", "RSI_DIR", "~/rsi/")
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "config"
        rsi_xml = etree.Element("output")
        rsi_xml.text = "RSI text"
        mock_dev.rpc.get_support_information.return_value = rsi_xml
        mock_dev.facts = {
            "personality": "MX",
            "model": "MX204",
            "model_info": {"MX204": {}},
            "hostname": "test-host",
            "srx_cluster": None,
        }

        m = mock_open()
        with patch.object(rsi.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}):
            with patch("builtins.open", m):
                result = rsi.cmd_rsi("test-host")

        assert result == 0
        expected_dir = os.path.expanduser("~/rsi/")
        m.assert_any_call(f"{expected_dir}test-host.SCF", mode="w")
        m.assert_any_call(f"{expected_dir}test-host.RSI", mode="w")
