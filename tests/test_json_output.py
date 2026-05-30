"""--json machine-readable output tests.

Covers the display-layer JSON serializers and the CLI integration that
must keep stdout pure JSON (logs rerouted to stderr) so the output is
safe to pipe into jq / Ansible.
"""

import datetime
import json
import logging
import sys
from unittest.mock import MagicMock, patch

from junos_ops import cli
from junos_ops import common
from junos_ops import display


class TestFormatJson:
    """display.format_json / format_json_obj unit tests."""

    def test_dict_result_merges_hostname(self):
        """A dict result is surfaced with a top-level hostname key."""
        line = display.format_json("rt1", {"ok": True, "running": "22.4R3"})
        obj = json.loads(line)
        assert obj == {"hostname": "rt1", "ok": True, "running": "22.4R3"}

    def test_non_dict_result_wrapped(self):
        """A non-dict result is wrapped under a result key."""
        obj = json.loads(display.format_json("rt1", "raw text"))
        assert obj == {"hostname": "rt1", "result": "raw text"}

    def test_single_line(self):
        """The serialized form is exactly one line (JSONL-safe)."""
        line = display.format_json("rt1", {"a": 1, "b": {"c": 2}})
        assert "\n" not in line

    def test_non_ascii_preserved(self):
        """ensure_ascii=False keeps non-ASCII text readable, not escaped."""
        line = display.format_json("rt1", {"diff": "ポート設定"})
        assert "ポート設定" in line
        assert json.loads(line)["diff"] == "ポート設定"

    def test_non_serializable_falls_back_to_str(self):
        """default=str rescues values json cannot natively serialize."""
        obj = json.loads(
            display.format_json("rt1", {"when": datetime.date(2026, 5, 31)})
        )
        assert obj["when"] == "2026-05-31"

    def test_format_json_obj_no_hostname_injection(self):
        """format_json_obj emits the object as-is (no hostname key added)."""
        obj = json.loads(display.format_json_obj({"model": "EX2300", "ok": True}))
        assert obj == {"model": "EX2300", "ok": True}


class TestRouteLogsToStderr:
    """_route_logs_to_stderr moves stdout handlers, leaves others alone."""

    def test_stdout_handler_moved_to_stderr(self):
        root = logging.getLogger()
        h = logging.StreamHandler(sys.stdout)
        root.addHandler(h)
        try:
            cli._route_logs_to_stderr()
            assert h.stream is sys.stderr
        finally:
            root.removeHandler(h)

    def test_non_stdout_handler_untouched(self):
        root = logging.getLogger()
        sink = MagicMock()
        h = logging.StreamHandler(sink)
        root.addHandler(h)
        try:
            cli._route_logs_to_stderr()
            assert h.stream is sink
        finally:
            root.removeHandler(h)


class TestCmdJsonOutput:
    """cmd_* honour --json and keep stdout machine-parseable."""

    def test_cmd_version_emits_json(self, capsys, mock_args, mock_config):
        mock_args.json = True
        dev = MagicMock()
        result = {
            "hostname": "test-host",
            "ok": True,
            "model": "EX2300-24T",
            "running": "22.4R3",
        }
        with (
            patch.object(
                cli.common, "connect", return_value={"ok": True, "dev": dev}
            ),
            patch.object(cli.upgrade, "show_version", return_value=result),
        ):
            rc = cli.cmd_version("test-host")
        assert rc == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["hostname"] == "test-host"
        assert obj["running"] == "22.4R3"

    def test_connect_error_emits_json(self, capsys, mock_args, mock_config):
        mock_args.json = True
        with patch.object(
            cli.common,
            "connect",
            return_value={
                "ok": False,
                "error": "ConnectTimeoutError",
                "error_message": "timed out",
            },
        ):
            rc = cli.cmd_version("test-host")
        assert rc == 1
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj == {
            "hostname": "test-host",
            "ok": False,
            "phase": "connect",
            "error": "ConnectTimeoutError",
            "error_message": "timed out",
        }

    def test_worker_exception_emits_json_error(self, capsys, mock_args, mock_config):
        mock_args.json = True
        dev = MagicMock()
        with (
            patch.object(
                cli.common, "connect", return_value={"ok": True, "dev": dev}
            ),
            patch.object(
                cli.upgrade, "show_version", side_effect=RuntimeError("boom")
            ),
        ):
            rc = cli.cmd_version("test-host")
        assert rc == 1
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj["ok"] is False
        assert obj["error"] == "RuntimeError"
        assert obj["error_message"] == "boom"

    def test_config_json_stdout_stays_pure_json(self, capsys, mock_args, mock_config):
        """The headline guarantee: even when load_config streams progress via
        logger.info, --json keeps stdout 100% JSON by rerouting logs to stderr.
        """
        mock_args.json = True
        mock_args.configfile = "commands.set"
        # Attach a console handler to the SAME stdout capsys captures, so a
        # regression (logs left on stdout) would corrupt the captured output.
        root = logging.getLogger()
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        root.addHandler(console)

        dev = MagicMock()
        mock_cu = MagicMock()
        mock_cu.diff.return_value = "[edit]\n+  set system host-name test"
        uptime_xml = MagicMock()
        current_time = MagicMock()
        current_time.text = "2026-05-31 12:00:00 JST"
        uptime_xml.findtext.return_value = "2026-05-31 12:00:00 JST"
        uptime_xml.find.return_value = current_time
        dev.rpc.get_system_uptime_information.return_value = uptime_xml
        try:
            cli._route_logs_to_stderr()  # the fix under test
            with (
                patch.object(
                    cli.common, "connect", return_value={"ok": True, "dev": dev}
                ),
                patch("junos_ops.upgrade.Config", return_value=mock_cu),
                patch.object(
                    common,
                    "load_commands",
                    return_value=["set system host-name test"],
                ),
            ):
                rc = cli.cmd_config("test-host")
        finally:
            root.removeHandler(console)

        assert rc == 0
        captured = capsys.readouterr()
        out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        # Every stdout line must be valid JSON — this is the machine-readable
        # contract. If any logger.info leaked to stdout, json.loads raises.
        assert out_lines, "expected at least one JSON line on stdout"
        for ln in out_lines:
            json.loads(ln)
