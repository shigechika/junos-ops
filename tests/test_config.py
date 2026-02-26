"""設定読込・モデル取得・ハッシュキャッシュのテスト"""

import configparser
import os
import threading

import pytest


class TestGetDefaultConfig:
    """get_default_config() のテスト"""

    def test_current_dir(self, junos_common, tmp_path, monkeypatch):
        """カレントディレクトリの config.ini を優先する"""
        (tmp_path / "config.ini").write_text("[DEFAULT]\n")
        monkeypatch.chdir(tmp_path)
        result = junos_common.get_default_config()
        assert result == "config.ini"

    def test_xdg_config_home(self, junos_common, tmp_path, monkeypatch):
        """XDG_CONFIG_HOME 配下の config.ini を検出する"""
        xdg_dir = tmp_path / "xdg" / "junos-ops"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "config.ini").write_text("[DEFAULT]\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.chdir(tmp_path / "xdg")  # config.ini がないディレクトリ
        result = junos_common.get_default_config()
        assert result == str(xdg_dir / "config.ini")

    def test_fallback(self, junos_common, tmp_path, monkeypatch):
        """どこにも見つからない場合は config.ini を返す"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        result = junos_common.get_default_config()
        assert result == "config.ini"


class TestReadConfig:
    """read_config() のテスト"""

    def test_valid_config(self, junos_common, mock_args, tmp_path):
        """正常なINIファイルを読み込める"""
        ini = tmp_path / "test.ini"
        ini.write_text(
            "[DEFAULT]\n"
            "id = testuser\n"
            "port = 830\n"
            "\n"
            "[host1.example.jp]\n"
            "\n"
            "[host2.example.jp]\n"
            "host = 192.0.2.1\n"
        )
        junos_common.args.config = str(ini)
        result = junos_common.read_config()
        assert result is False
        assert junos_common.config.has_section("host1.example.jp")
        assert junos_common.config.has_section("host2.example.jp")

    def test_empty_config(self, junos_common, mock_args, tmp_path):
        """空のINIファイルはエラー（True）を返す"""
        ini = tmp_path / "empty.ini"
        ini.write_text("")
        junos_common.args.config = str(ini)
        result = junos_common.read_config()
        assert result is True

    def test_host_default_to_section_name(self, junos_common, mock_args, tmp_path):
        """hostキー省略時にセクション名がhostとして設定される"""
        ini = tmp_path / "test.ini"
        ini.write_text(
            "[DEFAULT]\nid = testuser\nport = 830\n\n"
            "[rt1.example.jp]\n"
        )
        junos_common.args.config = str(ini)
        junos_common.read_config()
        assert junos_common.config.get("rt1.example.jp", "host") == "rt1.example.jp"

    def test_host_override(self, junos_common, mock_args, tmp_path):
        """hostキー指定時はその値が使われる"""
        ini = tmp_path / "test.ini"
        ini.write_text(
            "[DEFAULT]\nid = testuser\nport = 830\n\n"
            "[rt2.example.jp]\n"
            "host = 192.0.2.1\n"
        )
        junos_common.args.config = str(ini)
        junos_common.read_config()
        assert junos_common.config.get("rt2.example.jp", "host") == "192.0.2.1"


class TestGetModelFile:
    """get_model_file() のテスト"""

    def test_found(self, junos_upgrade, mock_config):
        result = junos_upgrade.get_model_file("test-host", "EX2300-24T")
        assert result == "junos-arm-32-22.4R3-S6.5.tgz"

    def test_not_found(self, junos_upgrade, mock_config):
        """存在しないモデルは例外を raise"""
        with pytest.raises(configparser.NoOptionError):
            junos_upgrade.get_model_file("test-host", "UNKNOWN_MODEL")

    def test_case_insensitive(self, junos_upgrade, mock_config):
        """モデル名は小文字に変換される"""
        result = junos_upgrade.get_model_file("test-host", "ex2300-24t")
        assert result == "junos-arm-32-22.4R3-S6.5.tgz"


class TestGetLocalPath:
    """get_local_path() のテスト"""

    def test_with_lpath(self, junos_upgrade, mock_config):
        """lpath 設定時はパスを結合する"""
        from junos_ops import common
        common.config.set("DEFAULT", "lpath", "/home/user/firmware")
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == "/home/user/firmware/junos-arm-32-22.4R3-S6.5.tgz"

    def test_without_lpath(self, junos_upgrade, mock_config):
        """lpath 未設定時はファイル名のみ返す（既存互換）"""
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == "junos-arm-32-22.4R3-S6.5.tgz"

    def test_empty_lpath(self, junos_upgrade, mock_config):
        """lpath が空文字の場合はファイル名のみ返す"""
        from junos_ops import common
        common.config.set("DEFAULT", "lpath", "")
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == "junos-arm-32-22.4R3-S6.5.tgz"

    def test_host_override_lpath(self, junos_upgrade, mock_config):
        """ホストセクションで lpath を上書き"""
        from junos_ops import common
        common.config.set("DEFAULT", "lpath", "/default/path")
        common.config.set("test-host", "lpath", "/host/specific/path")
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == "/host/specific/path/junos-arm-32-22.4R3-S6.5.tgz"

    def test_trailing_slash(self, junos_upgrade, mock_config):
        """末尾スラッシュがあっても正しく結合"""
        from junos_ops import common
        common.config.set("DEFAULT", "lpath", "/home/user/firmware/")
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == "/home/user/firmware/junos-arm-32-22.4R3-S6.5.tgz"

    def test_tilde_expansion(self, junos_upgrade, mock_config):
        """~ がホームディレクトリに展開される（sshkey と同じ仕様）"""
        from junos_ops import common
        common.config.set("DEFAULT", "lpath", "~/firmware")
        result = junos_upgrade.get_local_path("test-host", "junos-arm-32-22.4R3-S6.5.tgz")
        assert result == os.path.expanduser("~/firmware/junos-arm-32-22.4R3-S6.5.tgz")
        assert "~" not in result


class TestGetModelHash:
    """get_model_hash() のテスト"""

    def test_found(self, junos_upgrade, mock_config):
        result = junos_upgrade.get_model_hash("test-host", "EX2300-24T")
        assert result == "abc123def456"

    def test_not_found(self, junos_upgrade, mock_config):
        """存在しないモデルは例外を raise"""
        with pytest.raises(configparser.NoOptionError):
            junos_upgrade.get_model_hash("test-host", "UNKNOWN_MODEL")


class TestHashcache:
    """get_hashcache() / set_hashcache() のテスト"""

    def test_set_and_get(self, junos_upgrade, mock_config):
        junos_upgrade.set_hashcache("test-host", "testfile.tgz", "hash123")
        result = junos_upgrade.get_hashcache("test-host", "testfile.tgz")
        assert result == "hash123"

    def test_get_nonexistent_section(self, junos_upgrade, mock_config):
        """存在しないセクションは None を返す"""
        result = junos_upgrade.get_hashcache("nonexistent-host", "file.tgz")
        assert result is None

    def test_get_nonexistent_option(self, junos_upgrade, mock_config):
        """存在しないオプションは None を返す"""
        result = junos_upgrade.get_hashcache("test-host", "nofile.tgz")
        assert result is None

    def test_set_creates_section(self, junos_upgrade, mock_config):
        """存在しないセクションは自動作成される"""
        junos_upgrade.set_hashcache("new-host", "file.tgz", "hash456")
        result = junos_upgrade.get_hashcache("new-host", "file.tgz")
        assert result == "hash456"

    def test_thread_safety(self, junos_upgrade, mock_config):
        """複数スレッドからの同時アクセスでデータが壊れない"""
        errors = []

        def worker(host_id):
            try:
                host = f"thread-host-{host_id}"
                for i in range(50):
                    junos_upgrade.set_hashcache(host, "file.tgz", f"hash-{host_id}-{i}")
                    val = junos_upgrade.get_hashcache(host, "file.tgz")
                    if val is None:
                        errors.append(f"{host}: got None")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"
