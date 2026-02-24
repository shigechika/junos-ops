"""バージョン関連関数のテスト"""

import argparse
import datetime
from unittest.mock import MagicMock, patch

import pytest
from lxml import etree


class TestCompareVersion:
    """compare_version() のテスト"""

    def test_greater(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("22.4R3-S6", "22.4R3-S5") == 1

    def test_less(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("18.4R3-S9", "18.4R3-S10") == -1

    def test_equal(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("22.4R3-S6", "22.4R3-S6") == 0

    def test_none_left(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version(None, "22.4R3-S6") is None

    def test_none_right(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("22.4R3-S6", None) is None

    def test_both_none(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version(None, None) is None

    def test_major_version_diff(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("22.4R3-S6", "18.4R3-S10") == 1

    def test_minor_version_diff(self, junos_upgrade, mock_args):
        assert junos_upgrade.compare_version("22.2R1", "22.4R1") == -1


class TestYymmddhhmmType:
    """yymmddhhmm_type() のテスト"""

    def test_valid(self, junos_upgrade):
        result = junos_upgrade.yymmddhhmm_type("2501020304")
        assert result == datetime.datetime(2025, 1, 2, 3, 4)

    def test_year_end(self, junos_upgrade):
        result = junos_upgrade.yymmddhhmm_type("2512311959")
        assert result == datetime.datetime(2025, 12, 31, 19, 59)

    def test_invalid_format(self, junos_upgrade):
        with pytest.raises(argparse.ArgumentTypeError):
            junos_upgrade.yymmddhhmm_type("invalid")

    def test_empty_string(self, junos_upgrade):
        with pytest.raises(argparse.ArgumentTypeError):
            junos_upgrade.yymmddhhmm_type("")


class TestGetPlanningVersion:
    """get_planning_version() のテスト"""

    def test_normal(self, junos_upgrade, mock_args, mock_config):
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T"}
        result = junos_upgrade.get_planning_version("test-host", dev)
        assert result == "22.4R3-S6.5"

    def test_different_format(self, junos_upgrade, mock_args, mock_config):
        """SRX系のファイル名からもバージョン抽出できる"""
        mock_config.set("DEFAULT", "srx345.file", "junos-srxsme-15.1X49-D240.tgz")
        mock_config.set("DEFAULT", "srx345.hash", "dummy")
        dev = MagicMock()
        dev.facts = {"model": "SRX345"}
        result = junos_upgrade.get_planning_version("test-host", dev)
        assert result == "15.1X49-D240"

    def test_no_match(self, junos_upgrade, mock_args, mock_config):
        """正規表現にマッチしないファイル名の場合 None"""
        mock_config.set("DEFAULT", "srx345.file", "noversion.tgz")
        mock_config.set("DEFAULT", "srx345.hash", "dummy")
        dev = MagicMock()
        dev.facts = {"model": "SRX345"}
        result = junos_upgrade.get_planning_version("test-host", dev)
        assert result is None


class TestGetCommitInformation:
    """get_commit_information() のテスト"""

    def _make_commit_xml(self, sequence="0", seconds="1692679960",
                         dt_text="2023-08-22 13:12:40 JST",
                         user="admin", client="cli"):
        """テスト用のコミット情報 XML を生成する"""
        root = etree.Element("commit-information")
        history = etree.SubElement(root, "commit-history")
        seq = etree.SubElement(history, "sequence-number")
        seq.text = sequence
        dt = etree.SubElement(history, "date-time")
        dt.set("seconds", seconds)
        dt.text = dt_text
        u = etree.SubElement(history, "user")
        u.text = user
        c = etree.SubElement(history, "client")
        c.text = client
        return root

    def test_success(self, junos_upgrade, mock_args):
        """正常系: sequence 0 のコミット情報を取得"""
        dev = MagicMock()
        dev.rpc.get_commit_information.return_value = self._make_commit_xml()
        result = junos_upgrade.get_commit_information(dev)
        assert result is not None
        epoch, dt_str, user, client = result
        assert epoch == 1692679960
        assert dt_str == "2023-08-22 13:12:40 JST"
        assert user == "admin"
        assert client == "cli"

    def test_multiple_entries(self, junos_upgrade, mock_args):
        """複数コミット: sequence 0 のみを返す"""
        root = etree.Element("commit-information")
        # sequence 0
        h0 = etree.SubElement(root, "commit-history")
        etree.SubElement(h0, "sequence-number").text = "0"
        dt0 = etree.SubElement(h0, "date-time")
        dt0.set("seconds", "2000000000")
        dt0.text = "2033-05-18 00:00:00 JST"
        etree.SubElement(h0, "user").text = "admin"
        etree.SubElement(h0, "client").text = "cli"
        # sequence 1
        h1 = etree.SubElement(root, "commit-history")
        etree.SubElement(h1, "sequence-number").text = "1"
        dt1 = etree.SubElement(h1, "date-time")
        dt1.set("seconds", "1000000000")
        dt1.text = "2001-09-09 00:00:00 JST"
        etree.SubElement(h1, "user").text = "root"
        etree.SubElement(h1, "client").text = "netconf"

        dev = MagicMock()
        dev.rpc.get_commit_information.return_value = root
        result = junos_upgrade.get_commit_information(dev)
        assert result is not None
        epoch, _, user, _ = result
        assert epoch == 2000000000
        assert user == "admin"

    def test_no_history(self, junos_upgrade, mock_args):
        """コミット履歴なし → None"""
        root = etree.Element("commit-information")
        dev = MagicMock()
        dev.rpc.get_commit_information.return_value = root
        result = junos_upgrade.get_commit_information(dev)
        assert result is None

    def test_rpc_error(self, junos_upgrade, mock_args):
        """RPC エラー → None"""
        from jnpr.junos.exception import RpcError
        dev = MagicMock()
        dev.rpc.get_commit_information.side_effect = RpcError()
        result = junos_upgrade.get_commit_information(dev)
        assert result is None


class TestGetRescueConfigTime:
    """get_rescue_config_time() のテスト"""

    def _make_file_list_xml(self, seconds="1692679000"):
        """テスト用の file-list XML を生成する"""
        root = etree.Element("directory")
        file_info = etree.SubElement(root, "file-information")
        etree.SubElement(file_info, "file-name").text = "/config/rescue.conf.gz"
        file_date = etree.SubElement(file_info, "file-date")
        file_date.set("seconds", seconds)
        file_date.text = "Aug 22 12:50"
        return root

    def test_success(self, junos_upgrade, mock_args):
        """正常系: epoch 秒を取得"""
        dev = MagicMock()
        dev.rpc.file_list.return_value = self._make_file_list_xml("1692679000")
        result = junos_upgrade.get_rescue_config_time(dev)
        assert result == 1692679000

    def test_no_file(self, junos_upgrade, mock_args):
        """ファイルなし → None"""
        root = etree.Element("directory")
        etree.SubElement(root, "output").text = "/config/rescue.conf.gz: No such file or directory"
        dev = MagicMock()
        dev.rpc.file_list.return_value = root
        result = junos_upgrade.get_rescue_config_time(dev)
        assert result is None

    def test_rpc_error(self, junos_upgrade, mock_args):
        """RPC エラー → None"""
        from jnpr.junos.exception import RpcError
        dev = MagicMock()
        dev.rpc.file_list.side_effect = RpcError()
        result = junos_upgrade.get_rescue_config_time(dev)
        assert result is None


class TestShowVersionWithoutFile:
    """show_version() が .file 未定義でもエラーにならないテスト (Issue #37)"""

    def _make_dev(self):
        """upgrade 用 .file を持たないデバイスの MagicMock"""
        dev = MagicMock()
        dev.facts = {
            "hostname": "sw1",
            "model": "EX2300-24T",
            "version": "22.4R3-S6.5",
            "personality": "SWITCH",
        }
        # get_pending_version で使う RPC
        rpc_xml = etree.Element("software-information")
        etree.SubElement(rpc_xml, "output").text = "no pending"
        dev.rpc.get_software_information.return_value = rpc_xml
        # get_commit_information
        dev.rpc.get_commit_information.return_value = etree.Element(
            "commit-information"
        )
        # get_reboot_information
        reboot_xml = etree.Element("system-reboot-information")
        out = etree.SubElement(reboot_xml, "output")
        out.text = "No shutdown/reboot scheduled."
        dev.rpc.get_reboot_information.return_value = reboot_xml
        return dev

    def _make_config_without_file(self, junos_common):
        """upgrade 用 .file/.hash を持たない config"""
        import configparser
        cfg = configparser.ConfigParser(allow_no_value=True)
        cfg.read_dict(
            {
                "DEFAULT": {
                    "id": "testuser",
                    "pw": "testpass",
                    "sshkey": "id_ed25519",
                    "port": "830",
                    "hashalgo": "md5",
                    "rpath": "/var/tmp",
                },
                "sw1.example.com": {"host": "192.0.2.1"},
            }
        )
        junos_common.config = cfg

    def test_no_file_option_returns_false(self, junos_upgrade, junos_common, mock_args):
        """.file 未定義でも show_version は False（正常終了）を返す"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        result = junos_upgrade.show_version("sw1.example.com", dev)
        assert result is False

    def test_no_file_option_shows_version(self, junos_upgrade, junos_common,
                                           mock_args, capsys):
        """.file 未定義でもホスト名・モデル・バージョンは表示される"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        junos_upgrade.show_version("sw1.example.com", dev)
        captured = capsys.readouterr()
        assert "sw1" in captured.out
        assert "EX2300-24T" in captured.out
        assert "22.4R3-S6.5" in captured.out

    def test_no_file_option_planning_none(self, junos_upgrade, junos_common,
                                           mock_args, capsys):
        """.file 未定義時 planning version は None 表示"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        junos_upgrade.show_version("sw1.example.com", dev)
        captured = capsys.readouterr()
        assert "planning version: None" in captured.out
