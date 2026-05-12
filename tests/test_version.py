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


class TestGetPendingInstallTime:
    """get_pending_install_time() のテスト (issue #54 根本対策)"""

    def _make_dir_listing(self, entries):
        """複数 file-information を持つ directory XML を作る。entries: list of (name, seconds)。"""
        root = etree.Element("directory")
        for name, seconds in entries:
            fi = etree.SubElement(root, "file-information")
            etree.SubElement(fi, "file-name").text = name
            fd = etree.SubElement(fi, "file-date")
            fd.set("seconds", seconds)
            fd.text = "Apr 16 12:00"
        return root

    def test_single_file(self, junos_upgrade, mock_args):
        """1ファイルだけの時はその mtime を返す"""
        dev = MagicMock()
        dev.rpc.file_list.return_value = self._make_dir_listing(
            [("junos-arm-32-23.4R2-S7.4.tgz", "1713200000")]
        )
        assert junos_upgrade.get_pending_install_time(dev) == 1713200000

    def test_picks_newest(self, junos_upgrade, mock_args):
        """複数ファイルがあれば最新の mtime を返す"""
        dev = MagicMock()
        dev.rpc.file_list.return_value = self._make_dir_listing([
            ("junos-old.tgz", "1000000000"),
            ("junos-new.tgz", "1713200000"),
            ("junos-mid.tgz", "1500000000"),
        ])
        assert junos_upgrade.get_pending_install_time(dev) == 1713200000

    def test_empty_directory(self, junos_upgrade, mock_args):
        """ファイルなし → None"""
        dev = MagicMock()
        dev.rpc.file_list.return_value = etree.Element("directory")
        assert junos_upgrade.get_pending_install_time(dev) is None

    def test_rpc_error(self, junos_upgrade, mock_args):
        """RPC エラー → None"""
        from jnpr.junos.exception import RpcError
        dev = MagicMock()
        dev.rpc.file_list.side_effect = RpcError()
        assert junos_upgrade.get_pending_install_time(dev) is None

    def test_non_integer_seconds(self, junos_upgrade, mock_args):
        """seconds が int に parse できないエントリは無視"""
        dev = MagicMock()
        dev.rpc.file_list.return_value = self._make_dir_listing([
            ("bogus.tgz", "notanumber"),
            ("good.tgz", "1713200000"),
        ])
        assert junos_upgrade.get_pending_install_time(dev) == 1713200000


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

    def test_no_file_returns_dict(self, junos_upgrade, junos_common, mock_args):
        """.file 未定義でも show_version は dict を返し、エラーにならない"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        result = junos_upgrade.show_version("sw1.example.com", dev)
        assert isinstance(result, dict)
        assert result["hostname"] == "sw1"
        assert result["model"] == "EX2300-24T"
        assert result["running"] == "22.4R3-S6.5"

    def test_no_file_planning_is_none(self, junos_upgrade, junos_common, mock_args):
        """.file 未定義時 planning は None"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        result = junos_upgrade.show_version("sw1.example.com", dev)
        assert result["planning"] is None
        assert result["running_vs_planning"] is None

    def test_no_file_local_remote_none(self, junos_upgrade, junos_common, mock_args):
        """.file 未定義時 local_package / remote_package は None"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        result = junos_upgrade.show_version("sw1.example.com", dev)
        assert result["local_package"] is None
        assert result["remote_package"] is None

    def test_pending_none_and_commit_none(self, junos_upgrade, junos_common, mock_args):
        """pending なし & commit 情報なし → config_changed_after_install は False"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        result = junos_upgrade.show_version("sw1.example.com", dev)
        assert result["pending"] is None
        assert result["commit"] is None
        assert result["config_changed_after_install"] is False

    def test_does_not_print(self, junos_upgrade, junos_common, mock_args, capsys):
        """show_version は標準出力に何も print しない"""
        self._make_config_without_file(junos_common)
        dev = self._make_dev()
        junos_upgrade.show_version("sw1.example.com", dev)
        captured = capsys.readouterr()
        assert captured.out == ""


# --- _pending_from_install_log / get_pending_version ---


def _make_install_log_rpc(body_text):
    """Build an XML reply matching what ``get_log filename=install`` returns.

    The parser relies on ``etree.tostring(rpc, encoding='unicode')`` which
    HTML-escapes nested tag literals to ``&lt;...&gt;``. We mimic that by
    putting the raw CLI text (containing literal ``<output>`` /
    ``<package-result>`` tags) into a single text node so etree escapes
    them when serializing.
    """
    el = etree.Element("file-content")
    el.text = body_text
    return el


# Last <output> block excerpt from a QFX5110-48S-4C running
# jinstall-host-qfx-5e (host-based) — captured from kudan-rt 2026-05-12.
QFX5E_INSTALL_LOG_TAIL = """\
2026-05-12 12:18:29 JST mgd[59821]: /usr/libexec/ui/package -X update -no-validate /var/tmp/jinstall-host-qfx-5e-x86-64-23.4R2-S7.7-secure-signed.tgz
<output>
Verified jinstall-host-qfx-5e-x86-64-23.4R2-S7.7-secure-signed signed by ...
upgrade_platform: Staging the upgrade package - /var/tmp/jinstall-qfx-5e-junos-23.4R2-S7.7-secure-linux.tgz..
upgrade_platform: Checksum verified and OK...
upgrade_platform: Staging of /var/tmp/jinstall-qfx-5e-junos-23.4R2-S7.7-secure-linux.tgz completed
upgrade_platform: System need *REBOOT* to complete the upgrade
Host OS upgrade staged. Reboot the system to complete installation!

Install completed
</output>
<package-result>0</package-result>
"""

# SRX1500 install log excerpt (existing SRX_MIDRANGE path — regression).
SRX_MIDRANGE_INSTALL_LOG_TAIL = """\
<output>
upgrade_platform: Staging the upgrade package - /var/tmp/junos-srxentedge-x86-64-20.4R3.8-linux.tgz..
upgrade_platform: Staging of /var/tmp/junos-srxentedge-x86-64-20.4R3.8-linux.tgz completed
</output>
<package-result>0</package-result>
"""


class TestPendingFromInstallLog:
    """_pending_from_install_log() の直接テスト"""

    def test_qfx_host_based(self, junos_upgrade):
        dev = MagicMock()
        dev.rpc.get_log.return_value = _make_install_log_rpc(QFX5E_INSTALL_LOG_TAIL)
        assert (
            junos_upgrade._pending_from_install_log("kudan-rt", dev)
            == "23.4R2-S7.7"
        )

    def test_srx_midrange(self, junos_upgrade):
        dev = MagicMock()
        dev.rpc.get_log.return_value = _make_install_log_rpc(
            SRX_MIDRANGE_INSTALL_LOG_TAIL
        )
        assert (
            junos_upgrade._pending_from_install_log("srx1", dev) == "20.4R3.8"
        )

    def test_picks_last_output_block(self, junos_upgrade):
        """複数の <output> がある場合、最後のブロックの version を返す"""
        body = (
            "<output>\n"
            "upgrade_platform: Staging of /var/tmp/jinstall-qfx-5e-junos-23.4R2-S6.6-secure-linux.tgz completed\n"
            "</output>\n"
            "<package-result>0</package-result>\n"
            + QFX5E_INSTALL_LOG_TAIL
        )
        dev = MagicMock()
        dev.rpc.get_log.return_value = _make_install_log_rpc(body)
        assert (
            junos_upgrade._pending_from_install_log("kudan-rt", dev)
            == "23.4R2-S7.7"
        )

    def test_package_result_nonzero_returns_none(self, junos_upgrade):
        body = (
            "<output>\n"
            "upgrade_platform: Staging of /var/tmp/jinstall-qfx-5e-junos-23.4R2-S7.7-secure-linux.tgz completed\n"
            "</output>\n"
            "<package-result>1</package-result>\n"
        )
        dev = MagicMock()
        dev.rpc.get_log.return_value = _make_install_log_rpc(body)
        assert junos_upgrade._pending_from_install_log("h", dev) is None

    def test_no_staging_line(self, junos_upgrade):
        dev = MagicMock()
        dev.rpc.get_log.return_value = _make_install_log_rpc(
            "<output>nothing relevant</output>\n"
        )
        assert junos_upgrade._pending_from_install_log("h", dev) is None

    def test_rpc_exception_returns_none(self, junos_upgrade):
        dev = MagicMock()
        dev.rpc.get_log.side_effect = RuntimeError("boom")
        assert junos_upgrade._pending_from_install_log("h", dev) is None


class TestGetPendingVersionSwitchFallback:
    """SWITCH personality: show version に Pending: 行が無いとき install log に fallback"""

    def _make_dev_qfx_host(self):
        dev = MagicMock()
        dev.facts = {
            "hostname": "kudan-rt",
            "model": "QFX5110-48S-4C",
            "version": "23.4R2-S6.6",
            "personality": "SWITCH",
        }
        # show version: no Pending: line (QFX5e host-based)
        sw_info = etree.Element("software-information")
        etree.SubElement(sw_info, "output").text = (
            "Hostname: kudan-rt\n"
            "Model: qfx5110-48s-4c\n"
            "Junos: 23.4R2-S6.6\n"
            "JUNOS Host qfx-5e base package [23.4R2-S6.6]\n"
        )
        dev.rpc.get_software_information.return_value = sw_info
        # install log: latest staging is 23.4R2-S7.7
        dev.rpc.get_log.return_value = _make_install_log_rpc(QFX5E_INSTALL_LOG_TAIL)
        return dev

    def _make_dev_qfx_classic(self):
        dev = MagicMock()
        dev.facts = {
            "hostname": "sw2",
            "model": "EX2300-24T",
            "version": "22.4R3-S6.5",
            "personality": "SWITCH",
        }
        sw_info = etree.Element("software-information")
        etree.SubElement(sw_info, "output").text = (
            "Hostname: sw2\nJunos: 22.4R3-S6.5\nPending: 22.4R3-S10\n"
        )
        dev.rpc.get_software_information.return_value = sw_info
        return dev

    def test_qfx_host_falls_back_to_install_log(self, junos_upgrade, mock_args):
        dev = self._make_dev_qfx_host()
        assert (
            junos_upgrade.get_pending_version("kudan-rt", dev) == "23.4R2-S7.7"
        )
        # confirm install log was actually consulted
        assert dev.rpc.get_log.called

    def test_classic_switch_uses_show_version(self, junos_upgrade, mock_args):
        """Pending: 行があるとき install log は読まれない"""
        dev = self._make_dev_qfx_classic()
        assert junos_upgrade.get_pending_version("sw2", dev) == "22.4R3-S10"
        assert not dev.rpc.get_log.called

    def test_qfx_host_no_pending_at_all(self, junos_upgrade, mock_args):
        """Pending: 行も install log も空なら None"""
        dev = self._make_dev_qfx_host()
        dev.rpc.get_log.return_value = _make_install_log_rpc("<output>idle</output>\n")
        assert junos_upgrade.get_pending_version("kudan-rt", dev) is None


class TestGetPendingVersionSrxMidrange:
    """SRX_MIDRANGE/HIGHEND の既存挙動が壊れていないことを確認"""

    def _make_dev(self, personality="SRX_MIDRANGE"):
        dev = MagicMock()
        dev.facts = {
            "hostname": "srx1",
            "model": "SRX1500",
            "version": "20.4R3.6",
            "personality": personality,
        }
        sw_info = etree.Element("software-information")
        etree.SubElement(sw_info, "output").text = "Hostname: srx1\nJunos: 20.4R3.6\n"
        dev.rpc.get_software_information.return_value = sw_info
        dev.rpc.get_log.return_value = _make_install_log_rpc(
            SRX_MIDRANGE_INSTALL_LOG_TAIL
        )
        return dev

    def test_srx_midrange(self, junos_upgrade, mock_args):
        dev = self._make_dev(personality="SRX_MIDRANGE")
        assert junos_upgrade.get_pending_version("srx1", dev) == "20.4R3.8"

    def test_srx_highend(self, junos_upgrade, mock_args):
        dev = self._make_dev(personality="SRX_HIGHEND")
        assert junos_upgrade.get_pending_version("srx1", dev) == "20.4R3.8"


class TestDisplayPrintVersion:
    """display.print_version() のテスト"""

    def _base_result(self):
        return {
            "hostname": "rt1",
            "model": "MX240",
            "running": "21.4R3-S5.4",
            "planning": "21.4R3-S5.4",
            "pending": None,
            "running_vs_planning": 0,
            "running_vs_pending": None,
            "commit": None,
            "rescue_config_epoch": None,
            "config_changed_after_install": False,
            "local_package": None,
            "remote_package": None,
            "reboot_scheduled": None,
        }

    def test_basic_fields(self, capsys):
        from junos_ops import display
        display.print_version(self._base_result())
        out = capsys.readouterr().out
        assert "- hostname: rt1" in out
        assert "- model: MX240" in out
        assert "- running version: 21.4R3-S5.4" in out
        assert "- planning version: 21.4R3-S5.4" in out
        assert "- pending version: None" in out

    def test_planning_less_than_running(self, capsys):
        from junos_ops import display
        r = self._base_result()
        r["planning"] = "22.4R3-S6"
        r["running_vs_planning"] = -1
        display.print_version(r)
        out = capsys.readouterr().out
        assert "running='21.4R3-S5.4' < planning='22.4R3-S6'" in out

    def test_pending_greater_than_running(self, capsys):
        from junos_ops import display
        r = self._base_result()
        r["pending"] = "22.4R3-S6"
        r["running_vs_pending"] = -1
        display.print_version(r)
        out = capsys.readouterr().out
        assert "Please plan to reboot" in out

    def test_commit_line(self, capsys):
        from junos_ops import display
        r = self._base_result()
        r["commit"] = {
            "epoch": 1692679960,
            "datetime": "2023-08-22 13:12:40 JST",
            "user": "admin",
            "client": "cli",
        }
        display.print_version(r)
        out = capsys.readouterr().out
        assert "last commit: 2023-08-22 13:12:40 JST by admin via cli" in out

    def test_config_changed_warning(self, capsys):
        from junos_ops import display
        r = self._base_result()
        r["commit"] = {
            "epoch": 1692679960,
            "datetime": "2023-08-22 13:12:40 JST",
            "user": "admin",
            "client": "cli",
        }
        r["pending"] = "22.4R3-S6"
        r["config_changed_after_install"] = True
        display.print_version(r)
        out = capsys.readouterr().out
        assert "WARNING: config modified after firmware install" in out

    def test_reboot_scheduled(self, capsys):
        from junos_ops import display
        r = self._base_result()
        r["reboot_scheduled"] = "reboot requested by admin at Sun Dec 5 01:00:00 2021"
        display.print_version(r)
        out = capsys.readouterr().out
        assert "reboot requested by admin" in out
