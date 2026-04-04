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
        assert result["ok"] is True
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
        assert result["ok"] is True
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
        assert result["ok"] is True
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
        assert result["ok"] is False
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
        assert result["ok"] is False
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
        assert result["ok"] is False
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
        assert result["ok"] is False
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
        assert result["ok"] is True
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
                    "set system ntp server 192.0.2.1",
                ],
            ) as mock_load_cmds,
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")

        assert result["ok"] is True
        # load_commands がファイルパスで呼ばれている
        mock_load_cmds.assert_called_once_with("commands.set")
        # cu.load() に文字列が渡されている（path= ではない）
        mock_cu.load.assert_called_once_with(
            "set system host-name test\nset system ntp server 192.0.2.1",
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

        assert result["ok"] is True
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
        assert result["ok"] is True
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
        assert result["ok"] is False
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
        assert result["ok"] is False
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
        assert result["ok"] is True
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
        assert result["ok"] is True
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
        assert result["ok"] is True
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
        assert result["ok"] is False
        assert dev.cli.call_count == 2
        mock_cu.commit.assert_called_once_with(confirm=1)
        mock_cu.unlock.assert_called_once()

    def test_health_check_rpc_success(self, junos_upgrade, mock_args, mock_config):
        """NETCONF RPC probe success → commit confirmed"""
        from lxml import etree

        mock_args.health_check = ["uptime"]
        dev = MagicMock()
        root = etree.Element("system-uptime-information")
        ct = etree.SubElement(root, "current-time")
        dt = etree.SubElement(ct, "date-time")
        dt.text = "2026-03-14 10:00:00 JST"
        dev.rpc.get_system_uptime_information.return_value = root
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result["ok"] is True
        dev.cli.assert_not_called()
        dev.rpc.get_system_uptime_information.assert_called_once()
        assert mock_cu.commit.call_count == 2

    def test_health_check_rpc_no_data(self, junos_upgrade, mock_args, mock_config):
        """RPC response has no current-time → failure"""
        from lxml import etree

        mock_args.health_check = ["uptime"]
        dev = MagicMock()
        root = etree.Element("system-uptime-information")
        dev.rpc.get_system_uptime_information.return_value = root
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result["ok"] is False
        mock_cu.commit.assert_called_once_with(confirm=1)

    def test_health_check_rpc_exception(self, junos_upgrade, mock_args, mock_config):
        """RPC exception → failure"""
        mock_args.health_check = ["uptime"]
        dev = MagicMock()
        dev.rpc.get_system_uptime_information.side_effect = Exception("NETCONF timeout")
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result["ok"] is False
        mock_cu.commit.assert_called_once_with(confirm=1)

    def test_health_check_rpc_fallback_to_ping(
        self, junos_upgrade, mock_args, mock_config
    ):
        """RPC fails → fallback to ping succeeds"""
        mock_args.health_check = ["uptime", "ping count 3 255.255.255.255 rapid"]
        dev = MagicMock()
        dev.rpc.get_system_uptime_information.side_effect = Exception("timeout")
        dev.cli.return_value = (
            "...3 packets transmitted, 3 packets received, 0% packet loss"
        )
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result["ok"] is True
        dev.rpc.get_system_uptime_information.assert_called_once()
        dev.cli.assert_called_once_with("ping count 3 255.255.255.255 rapid")
        assert mock_cu.commit.call_count == 2


class TestJinja2Template:
    """Jinja2 テンプレート機能のテスト (Issue #30)"""

    def test_j2_renders_template(self, junos_upgrade, mock_args, mock_config):
        """.j2 file triggers render_template instead of load_commands"""
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        rendered = ["set system host-name rt1", "set system ntp server 192.0.2.1"]
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(
                common, "render_template", return_value=rendered
            ) as mock_render,
        ):
            result = junos_upgrade.load_config(
                "test-host", dev, "commands.set.j2"
            )
        assert result["ok"] is True
        mock_render.assert_called_once_with("commands.set.j2", "test-host", dev)
        mock_cu.load.assert_called_once_with(
            "set system host-name rt1\nset system ntp server 192.0.2.1",
            format="set",
        )

    def test_non_j2_uses_load_commands(self, junos_upgrade, mock_args, mock_config):
        """Non-.j2 file uses load_commands as before"""
        dev = MagicMock()
        dev.cli.return_value = "3 packets transmitted, 3 packets received"
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(
                common, "load_commands", return_value=["set system ntp"]
            ) as mock_load,
        ):
            result = junos_upgrade.load_config(
                "test-host", dev, "commands.set"
            )
        assert result["ok"] is True
        mock_load.assert_called_once_with("commands.set")

    def test_render_template_variables(self, mock_config):
        """render_template injects var_ prefix, hostname, and facts"""
        mock_config.set("test-host", "var_ntp_server", "192.0.2.1")
        mock_config.set("test-host", "var_syslog_host", "192.0.2.2")
        dev = MagicMock()
        dev.facts = {"hostname": "rt1", "model": "MX240", "personality": "MX"}

        import tempfile
        import os

        template_content = (
            "set system host-name {{ hostname }}\n"
            "set system ntp server {{ ntp_server }}\n"
            "set system syslog host {{ syslog_host }} any warning\n"
            "# this is a comment\n"
            "\n"
            "set system model-note {{ facts.model }}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == [
            "set system host-name test-host",
            "set system ntp server 192.0.2.1",
            "set system syslog host 192.0.2.2 any warning",
            "set system model-note MX240",
        ]

    def test_render_template_strict_undefined(self, mock_config):
        """Undefined variable raises error"""
        from jinja2 import UndefinedError

        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write("set system ntp server {{ undefined_var }}\n")
            f.flush()
            try:
                import pytest

                with pytest.raises(UndefinedError):
                    common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

    def test_render_template_default_vars(self, mock_config):
        """DEFAULT section var_ keys are inherited by all hosts"""
        mock_config.set("DEFAULT", "var_ntp_server", "192.0.2.1")
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write("set system ntp server {{ ntp_server }}\n")
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == ["set system ntp server 192.0.2.1"]

    def test_j2_dry_run_shows_rendered(
        self, junos_upgrade, mock_args, mock_config, capsys
    ):
        """dry-run with .j2 shows rendered commands"""
        mock_args.dry_run = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        rendered = ["set system host-name rt1"]
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "render_template", return_value=rendered),
        ):
            result = junos_upgrade.load_config(
                "test-host", dev, "commands.set.j2"
            )
        assert result["ok"] is True
        # core は print しないので、steps / rendered_commands 経由で検証
        assert result["rendered_commands"] == rendered
        messages = " ".join(s.get("message", "") for s in result["steps"])
        assert "set system host-name rt1" in messages
        assert "dry-run" in messages
        assert capsys.readouterr().out == ""

    def test_render_template_switch_conditional(self, mock_config):
        """Jinja2 conditional on facts.personality"""
        mock_config.add_section("sw1.example.jp")
        mock_config.set("DEFAULT", "var_ntp_server", "192.0.2.1")
        dev = MagicMock()
        dev.facts = {"personality": "SWITCH"}

        import tempfile
        import os

        template_content = (
            "set system ntp server {{ ntp_server }}\n"
            "{% if facts.personality == 'SWITCH' %}\n"
            "set protocols vstp\n"
            "{% endif %}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "sw1.example.jp", dev)
            finally:
                os.unlink(f.name)

        assert "set protocols vstp" in result

    def test_render_template_router_no_vstp(self, mock_config):
        """Router personality does not include vstp"""
        mock_config.add_section("rt1.example.jp")
        mock_config.set("DEFAULT", "var_ntp_server", "192.0.2.1")
        dev = MagicMock()
        dev.facts = {"personality": "MX"}

        import tempfile
        import os

        template_content = (
            "set system ntp server {{ ntp_server }}\n"
            "{% if facts.personality == 'SWITCH' %}\n"
            "set protocols vstp\n"
            "{% endif %}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "rt1.example.jp", dev)
            finally:
                os.unlink(f.name)

        assert "set protocols vstp" not in result
        assert "set system ntp server 192.0.2.1" in result

    def test_render_template_import_error(self, mock_config):
        """Jinja2 not installed raises ImportError with install hint"""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "jinja2":
                raise ImportError("No module named 'jinja2'")
            return real_import(name, *args, **kwargs)

        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write("set system ntp\n")
            f.flush()
            try:
                import pytest

                with (
                    patch.object(builtins, "__import__", side_effect=mock_import),
                    pytest.raises(ImportError, match="pip install junos-ops"),
                ):
                    common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

    def test_render_template_file_not_found(self, mock_config):
        """Non-existent template file raises error"""
        dev = MagicMock()
        dev.facts = {}
        import pytest
        from jinja2 import TemplateNotFound

        with pytest.raises(TemplateNotFound):
            common.render_template(
                "/nonexistent/path/template.j2", "test-host", dev
            )

    def test_render_template_syntax_error(self, mock_config):
        """Jinja2 syntax error raises TemplateSyntaxError"""
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os
        import pytest
        from jinja2 import TemplateSyntaxError

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write("{% if facts.model %}\nset system\n")  # missing endif
            f.flush()
            try:
                with pytest.raises(TemplateSyntaxError):
                    common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

    def test_render_template_host_overrides_default(self, mock_config):
        """Host section var_ overrides DEFAULT var_"""
        mock_config.set("DEFAULT", "var_ntp_server", "192.0.2.1")
        mock_config.set("test-host", "var_ntp_server", "192.0.2.99")
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write("set system ntp server {{ ntp_server }}\n")
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == ["set system ntp server 192.0.2.99"]

    def test_render_template_jinja2_comment(self, mock_config):
        """{# Jinja2 comment #} is removed by Jinja2, not by # filter"""
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        template_content = (
            "{# This is a Jinja2 comment #}\n"
            "set system ntp server 192.0.2.1\n"
            "# This is a shell-style comment\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == ["set system ntp server 192.0.2.1"]

    def test_render_template_for_loop(self, mock_config):
        """{% for %} loop generates multiple commands"""
        mock_config.set("DEFAULT", "var_ntp_servers", "192.0.2.1,192.0.2.2,192.0.2.3")
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        template_content = (
            "{% for server in ntp_servers.split(',') %}\n"
            "set system ntp server {{ server }}\n"
            "{% endfor %}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == [
            "set system ntp server 192.0.2.1",
            "set system ntp server 192.0.2.2",
            "set system ntp server 192.0.2.3",
        ]

    def test_render_template_empty_result(self, mock_config):
        """Template that renders to only comments/blank lines returns empty"""
        dev = MagicMock()
        dev.facts = {}

        import tempfile
        import os

        template_content = (
            "{# all comments #}\n"
            "# another comment\n"
            "\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == []

    def test_render_template_facts_dict_access(self, mock_config):
        """facts accessible via both dot notation and bracket notation"""
        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T", "hostname": "sw1"}

        import tempfile
        import os

        template_content = (
            "set system model-note {{ facts.model }}\n"
            "set system host-note {{ facts['hostname'] }}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".j2", delete=False
        ) as f:
            f.write(template_content)
            f.flush()
            try:
                result = common.render_template(f.name, "test-host", dev)
            finally:
                os.unlink(f.name)

        assert result == [
            "set system model-note EX2300-24T",
            "set system host-note sw1",
        ]


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
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
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
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
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
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
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
            patch.object(cli.common, "connect", return_value={"hostname": "test-host", "host": "test-host", "ok": True, "dev": mock_dev, "error": None, "error_message": None}),
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
        assert result["ok"] is True
        # commit は1回だけ（confirm= なし）
        mock_cu.commit.assert_called_once_with()
        # ヘルスチェックは実行されない
        dev.cli.assert_not_called()

    def test_no_confirm_output(self, junos_upgrade, mock_args, mock_config, capsys):
        """--no-confirm では commit_mode=no_confirm と steps 内に文言"""
        mock_args.no_confirm = True
        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system ..."
        with (
            patch("junos_ops.upgrade.Config", return_value=mock_cu),
            patch.object(common, "load_commands", return_value=["set system ntp"]),
        ):
            result = junos_upgrade.load_config("test-host", dev, "commands.set")
        assert result["commit_mode"] == "no_confirm"
        messages = " ".join(s.get("message", "") for s in result["steps"])
        assert "commit applied (no confirm)" in messages
        # core は print しない
        assert capsys.readouterr().out == ""

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
        assert result["ok"] is False
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
        assert result["ok"] is True
        # commit confirmed + commit の2回
        assert mock_cu.commit.call_count == 2
        mock_cu.commit.assert_any_call(confirm=1)
