"""run_parallel() / get_targets() のテスト"""

import argparse
import configparser

import pytest


class TestRunParallel:
    """run_parallel() のテスト"""

    def test_serial(self, junos_common):
        """max_workers=1 でシリアル実行"""
        results = junos_common.run_parallel(lambda t: t.upper(), ["a", "b", "c"], max_workers=1)
        assert results == {"a": "A", "b": "B", "c": "C"}

    def test_parallel(self, junos_common):
        """max_workers>1 で並列実行"""
        results = junos_common.run_parallel(lambda t: t.upper(), ["a", "b", "c"], max_workers=3)
        assert results == {"a": "A", "b": "B", "c": "C"}

    def test_parallel_exception(self, junos_common):
        """並列実行中の例外はエラーコード1を返す"""
        def failing(t):
            if t == "b":
                raise RuntimeError("fail")
            return 0

        results = junos_common.run_parallel(failing, ["a", "b", "c"], max_workers=3)
        assert results["a"] == 0
        assert results["b"] == 1
        assert results["c"] == 0

    def test_empty_targets(self, junos_common):
        """空のターゲットリスト"""
        results = junos_common.run_parallel(lambda t: 0, [], max_workers=1)
        assert results == {}


class TestGetTargets:
    """get_targets() のテスト"""

    def test_all_sections(self, junos_common, mock_args, mock_config):
        """specialhosts 未指定時は全セクションを返す"""
        junos_common.args.specialhosts = []
        targets = junos_common.get_targets()
        assert targets == ["test-host"]

    def test_specific_hosts(self, junos_common, mock_args, mock_config):
        """specialhosts 指定時はそのリストを返す"""
        junos_common.args.specialhosts = ["test-host"]
        targets = junos_common.get_targets()
        assert targets == ["test-host"]

    def test_unknown_host_exits(self, junos_common, mock_args, mock_config):
        """存在しないホスト指定時は sys.exit"""
        junos_common.args.specialhosts = ["unknown-host"]
        with pytest.raises(SystemExit):
            junos_common.get_targets()


@pytest.fixture
def mock_config_with_tags(junos_common):
    """タグ付きの4ホスト設定"""
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
            "rt1.example.jp": {"host": "rt1.example.jp", "tags": "tokyo, core"},
            "rt2.example.jp": {"host": "rt2.example.jp", "tags": "osaka, core"},
            "sw1.example.jp": {"host": "sw1.example.jp", "tags": "tokyo, access"},
            "sw2.example.jp": {"host": "sw2.example.jp"},  # タグなし
        }
    )
    junos_common.config = cfg
    return cfg


class TestGetHostTags:
    """_get_host_tags() のテスト"""

    def test_tags_parsed(self, junos_common, mock_args, mock_config_with_tags):
        """タグがカンマ区切りで set に変換される"""
        tags = junos_common._get_host_tags("rt1.example.jp")
        assert tags == {"tokyo", "core"}

    def test_no_tags(self, junos_common, mock_args, mock_config_with_tags):
        """tags キーなしのホストは空 set"""
        tags = junos_common._get_host_tags("sw2.example.jp")
        assert tags == set()

    def test_case_insensitive(self, junos_common, mock_args, mock_config_with_tags):
        """タグは小文字に正規化される"""
        # 設定に大文字タグを追加してテスト
        junos_common.config.set("rt1.example.jp", "tags", "Tokyo, CORE")
        tags = junos_common._get_host_tags("rt1.example.jp")
        assert tags == {"tokyo", "core"}

    def test_whitespace_trimmed(self, junos_common, mock_args, mock_config_with_tags):
        """タグ前後の空白がトリムされる"""
        junos_common.config.set("rt1.example.jp", "tags", "  tokyo  ,  core  ")
        tags = junos_common._get_host_tags("rt1.example.jp")
        assert tags == {"tokyo", "core"}


class TestFilterByTagGroups:
    """Tests for _filter_by_tag_groups()."""

    def test_single_tag(self, junos_common, mock_args, mock_config_with_tags):
        """Single tag in one group filters to tokyo hosts."""
        matched = junos_common._filter_by_tag_groups([{"tokyo"}])
        assert matched == ["rt1.example.jp", "sw1.example.jp"]

    def test_and_within_group(self, junos_common, mock_args, mock_config_with_tags):
        """AND filter inside a group: tokyo AND core."""
        matched = junos_common._filter_by_tag_groups([{"tokyo", "core"}])
        assert matched == ["rt1.example.jp"]

    def test_or_between_groups(self, junos_common, mock_args, mock_config_with_tags):
        """OR between groups: {access} OR {core} matches hosts with either."""
        matched = junos_common._filter_by_tag_groups([{"access"}, {"core"}])
        # core: rt1, rt2; access: sw1 -> union preserves section order
        assert matched == ["rt1.example.jp", "rt2.example.jp", "sw1.example.jp"]

    def test_mixed_and_or(self, junos_common, mock_args, mock_config_with_tags):
        """(tokyo AND core) OR osaka."""
        matched = junos_common._filter_by_tag_groups([{"tokyo", "core"}, {"osaka"}])
        # tokyo AND core: rt1; osaka: rt2
        assert matched == ["rt1.example.jp", "rt2.example.jp"]

    def test_no_match(self, junos_common, mock_args, mock_config_with_tags):
        """No matching tag returns empty."""
        matched = junos_common._filter_by_tag_groups([{"nonexistent"}])
        assert matched == []


class TestGetTargetsWithTags:
    """get_targets() のタグフィルタリングテスト"""

    def test_tags_none_all_sections(self, junos_common, mock_args, mock_config_with_tags):
        """tags=None, hosts なし → 全セクション"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = None
        targets = junos_common.get_targets()
        assert targets == [
            "rt1.example.jp", "rt2.example.jp",
            "sw1.example.jp", "sw2.example.jp",
        ]

    def test_tags_none_with_hosts(self, junos_common, mock_args, mock_config_with_tags):
        """tags=None, hosts あり → 指定ホストのみ"""
        junos_common.args.specialhosts = ["rt1.example.jp"]
        junos_common.args.tags = None
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp"]

    def test_single_tag_filter(self, junos_common, mock_args, mock_config_with_tags):
        """--tags tokyo → tokyo タグを持つホスト"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "tokyo"
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp", "sw1.example.jp"]

    def test_and_tag_filter(self, junos_common, mock_args, mock_config_with_tags):
        """--tags tokyo,core → 両方のタグを持つホスト"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "tokyo,core"
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp"]

    def test_no_match_exits(self, junos_common, mock_args, mock_config_with_tags):
        """タグマッチなし & hosts なし → sys.exit"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "nonexistent"
        with pytest.raises(SystemExit):
            junos_common.get_targets()

    def test_tags_repeated_or(self, junos_common, mock_args, mock_config_with_tags):
        """--tags a --tags b: OR between groups."""
        junos_common.args.specialhosts = []
        junos_common.args.tags = ["access", "core"]
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp", "rt2.example.jp", "sw1.example.jp"]

    def test_tags_plus_hosts_intersection(self, junos_common, mock_args, mock_config_with_tags):
        """--tags core + hosts: intersection keeps only tag-matching names."""
        junos_common.args.specialhosts = ["rt1.example.jp", "sw2.example.jp"]
        junos_common.args.tags = "core"
        targets = junos_common.get_targets()
        # core tag is on rt1/rt2; only rt1 is also in the explicit list.
        assert targets == ["rt1.example.jp"]

    def test_tags_plus_hosts_preserves_order(self, junos_common, mock_args, mock_config_with_tags):
        """--tags + hosts: output order follows specialhosts order."""
        junos_common.args.specialhosts = ["sw1.example.jp", "rt1.example.jp"]
        junos_common.args.tags = "tokyo"
        targets = junos_common.get_targets()
        assert targets == ["sw1.example.jp", "rt1.example.jp"]

    def test_tags_plus_hosts_empty_intersection_exits(
        self, junos_common, mock_args, mock_config_with_tags
    ):
        """Empty intersection of tags and names exits with sys.exit."""
        junos_common.args.specialhosts = ["sw2.example.jp"]
        junos_common.args.tags = "tokyo"
        with pytest.raises(SystemExit):
            junos_common.get_targets()

    def test_case_insensitive_tags(self, junos_common, mock_args, mock_config_with_tags):
        """--tags は大文字小文字を区別しない"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "TOKYO,CORE"
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp"]

    def test_tags_whitespace(self, junos_common, mock_args, mock_config_with_tags):
        """--tags のスペースがトリムされる"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "  tokyo  ,  core  "
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp"]

    def test_intersection_unknown_host_exits(self, junos_common, mock_args, mock_config_with_tags):
        """Unknown host in intersection mode exits with sys.exit."""
        junos_common.args.specialhosts = ["unknown-host"]
        junos_common.args.tags = "tokyo"
        with pytest.raises(SystemExit):
            junos_common.get_targets()

    def test_core_tag_filter(self, junos_common, mock_args, mock_config_with_tags):
        """--tags core → core タグを持つホスト"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "core"
        targets = junos_common.get_targets()
        assert targets == ["rt1.example.jp", "rt2.example.jp"]

    def test_access_tag_filter(self, junos_common, mock_args, mock_config_with_tags):
        """--tags access → access タグを持つホスト"""
        junos_common.args.specialhosts = []
        junos_common.args.tags = "access"
        targets = junos_common.get_targets()
        assert targets == ["sw1.example.jp"]
