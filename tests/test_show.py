"""Tests for the show subcommand and the junos_ops.show core."""

import warnings
from unittest.mock import MagicMock, patch, call

import pytest
from jnpr.junos.exception import RpcTimeoutError
from lxml import etree

from junos_ops import cli, common, display, show


CONNECT_OK = {
    "hostname": "test-host",
    "host": "test-host",
    "ok": True,
    "dev": None,
    "error": None,
    "error_message": None,
}
CONNECT_FAIL = {
    "hostname": "test-host",
    "host": "test-host",
    "ok": False,
    "dev": None,
    "error": "ConnectError",
    "error_message": "mock connect error",
}


def _connect_ok(dev):
    return {**CONNECT_OK, "dev": dev}


class TestRunCli:
    """show.run_cli() returns a dict describing the outcome."""

    def test_text_success(self):
        dev = MagicMock()
        dev.cli.return_value = "output line\n"
        result = show.run_cli(dev, "show version", hostname="h")
        assert result["ok"] is True
        assert result["command"] == "show version"
        assert result["format"] == "text"
        assert result["output"] == "output line\n"
        assert result["error"] is None
        dev.cli.assert_called_once_with("show version")

    def test_json_passthrough(self):
        dev = MagicMock()
        dev.cli.return_value = {"interface-information": {"physical-interface": []}}
        result = show.run_cli(
            dev, "show interfaces terse", output_format="json", hostname="h"
        )
        assert result["ok"] is True
        assert result["format"] == "json"
        assert result["output"] == {
            "interface-information": {"physical-interface": []}
        }
        dev.cli.assert_called_once_with(
            "show interfaces terse", format="json"
        )

    def test_xml_pretty_printed(self):
        dev = MagicMock()
        dev.cli.return_value = etree.fromstring("<root><child>x</child></root>")
        result = show.run_cli(
            dev, "show version", output_format="xml", hostname="h"
        )
        assert result["ok"] is True
        assert result["format"] == "xml"
        # Serialised with pretty_print=True -> indented, str (not bytes).
        assert isinstance(result["output"], str)
        assert "<child>x</child>" in result["output"]
        assert "\n" in result["output"]
        dev.cli.assert_called_once_with("show version", format="xml")

    def test_invalid_format_raises(self):
        dev = MagicMock()
        with pytest.raises(ValueError, match="invalid format"):
            show.run_cli(dev, "show version", output_format="yaml")

    def test_generic_exception_returns_error_dict(self):
        dev = MagicMock()
        dev.cli.side_effect = RuntimeError("boom")
        result = show.run_cli(dev, "show version", hostname="h")
        assert result["ok"] is False
        assert result["output"] is None
        assert result["error"] == "RuntimeError"
        assert result["error_message"] == "boom"

    def test_suppresses_pyez_cli_debug_warning(self):
        """PyEZ's per-call RuntimeWarning must not leak from run_cli.

        PyEZ (device.py) emits the warning with a leading newline -
        see jnpr.junos.device.Device.cli - so the filter regex must tolerate it.
        """

        dev = MagicMock()

        def _cli_emitting_warning(*args, **kwargs):
            warnings.warn(
                "\nCLI command is for debug use only!\n"
                "Instead of:\ncli('show system alarms')\n"
                "Use:\nrpc.get_system_alarm_information()\n",
                RuntimeWarning,
            )
            return "No alarms currently active\n"

        dev.cli.side_effect = _cli_emitting_warning
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = show.run_cli(dev, "show system alarms", hostname="h")
        assert result["ok"] is True
        assert not any(
            "debug use only" in str(rec.message) for rec in caught
        ), [str(rec.message) for rec in caught]

    def test_unrelated_runtime_warning_still_surfaces(self):
        """Only the PyEZ debug warning is silenced; others pass through."""

        dev = MagicMock()

        def _cli_emitting_warning(*args, **kwargs):
            warnings.warn("something else entirely", RuntimeWarning)
            return "ok"

        dev.cli.side_effect = _cli_emitting_warning
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = show.run_cli(dev, "show version", hostname="h")
        assert result["ok"] is True
        assert any(
            "something else entirely" in str(rec.message) for rec in caught
        )


class TestRunCliRetry:
    """show.run_cli retries RpcTimeoutError only."""

    @patch("junos_ops.show.time.sleep")
    def test_retry_then_success(self, mock_sleep):
        dev = MagicMock()
        dev.cli.side_effect = [
            RpcTimeoutError(MagicMock(hostname="h"), "c", 30),
            "ok",
        ]
        result = show.run_cli(dev, "show system alarms", retry=2, hostname="h")
        assert result["ok"] is True
        assert result["output"] == "ok"
        assert dev.cli.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("junos_ops.show.time.sleep")
    def test_retry_exhausted_returns_error(self, mock_sleep):
        dev = MagicMock()
        dev.cli.side_effect = RpcTimeoutError(
            MagicMock(hostname="h"), "c", 30
        )
        result = show.run_cli(dev, "show ...", retry=2, hostname="h")
        assert result["ok"] is False
        assert result["error"] == "RpcTimeoutError"
        assert dev.cli.call_count == 3  # first call + 2 retries
        mock_sleep.assert_has_calls([call(5), call(10)])

    def test_no_retry_default(self):
        dev = MagicMock()
        dev.cli.side_effect = RpcTimeoutError(
            MagicMock(hostname="h"), "c", 30
        )
        result = show.run_cli(dev, "show ...", hostname="h")
        assert result["ok"] is False
        assert dev.cli.call_count == 1

    @patch("junos_ops.show.time.sleep")
    def test_non_timeout_not_retried(self, mock_sleep):
        dev = MagicMock()
        dev.cli.side_effect = RuntimeError("other")
        result = show.run_cli(dev, "show ...", retry=3, hostname="h")
        assert result["ok"] is False
        assert result["error"] == "RuntimeError"
        assert dev.cli.call_count == 1
        mock_sleep.assert_not_called()


class TestRunCliBatch:
    """show.run_cli_batch aggregates per-command results and short-circuits."""

    def test_all_succeed(self):
        dev = MagicMock()
        dev.cli.side_effect = ["a", "b"]
        result = show.run_cli_batch(
            dev, ["show a", "show b"], hostname="h"
        )
        assert result["ok"] is True
        assert [r["command"] for r in result["results"]] == ["show a", "show b"]
        assert [r["output"] for r in result["results"]] == ["a", "b"]

    def test_short_circuits_on_first_failure(self):
        dev = MagicMock()
        dev.cli.side_effect = [RuntimeError("boom"), "unreached"]
        result = show.run_cli_batch(
            dev, ["show a", "show b"], hostname="h"
        )
        assert result["ok"] is False
        assert len(result["results"]) == 1
        assert result["results"][0]["error"] == "RuntimeError"
        # show b must not be attempted after show a fails.
        assert dev.cli.call_count == 1


class TestFormatShow:
    """display.format_show renders text / json / xml appropriately."""

    def test_text_single(self):
        result = {
            "hostname": "h",
            "command": "show version",
            "format": "text",
            "ok": True,
            "output": "  Model: MX204  \n",
            "error": None,
            "error_message": None,
        }
        out = display.format_show(result)
        assert out.startswith("# h\n## show version\n")
        assert "Model: MX204" in out

    def test_json_single(self):
        result = {
            "hostname": "h",
            "command": "show interfaces",
            "format": "json",
            "ok": True,
            "output": {"greeting": "こんにちは"},
            "error": None,
            "error_message": None,
        }
        out = display.format_show(result)
        # ensure_ascii=False preserves non-ASCII.
        assert "こんにちは" in out
        assert '"greeting"' in out

    def test_batch_layout(self):
        result = {
            "hostname": "h",
            "format": "text",
            "ok": True,
            "results": [
                {
                    "hostname": "h",
                    "command": "show a",
                    "format": "text",
                    "ok": True,
                    "output": "out-a",
                    "error": None,
                    "error_message": None,
                },
                {
                    "hostname": "h",
                    "command": "show b",
                    "format": "text",
                    "ok": True,
                    "output": "out-b",
                    "error": None,
                    "error_message": None,
                },
            ],
        }
        out = display.format_show(result)
        assert "# h" in out
        assert "## show a" in out and "out-a" in out
        assert "## show b" in out and "out-b" in out

    def test_error_rendered(self):
        result = {
            "hostname": "h",
            "command": "show x",
            "format": "text",
            "ok": False,
            "output": None,
            "error": "RpcTimeoutError",
            "error_message": "timeout after 30s",
        }
        out = display.format_show(result)
        assert "timeout after 30s" in out


class TestCmdShow:
    """Integration tests via cli.cmd_show."""

    def test_connect_fail_returns_1(self, junos_common, mock_args, mock_config):
        mock_args.show_command = "show version"
        with patch.object(cli.common, "connect", return_value=CONNECT_FAIL):
            assert cli.cmd_show("test-host") == 1

    def test_text_success_prints_via_display(
        self, junos_common, mock_args, mock_config, capsys
    ):
        mock_args.show_command = "show version"
        mock_args.show_format = "text"
        dev = MagicMock()
        dev.cli.return_value = "Hostname: test-host\n"
        with patch.object(cli.common, "connect", return_value=_connect_ok(dev)):
            rc = cli.cmd_show("test-host")
        assert rc == 0
        dev.cli.assert_called_once_with("show version")
        dev.close.assert_called_once()
        captured = capsys.readouterr().out
        assert "# test-host" in captured
        assert "## show version" in captured
        assert "Hostname: test-host" in captured

    def test_json_flag_passes_format_kwarg(
        self, junos_common, mock_args, mock_config, capsys
    ):
        mock_args.show_command = "show interfaces terse"
        mock_args.show_format = "json"
        dev = MagicMock()
        dev.cli.return_value = {"interface-information": {}}
        with patch.object(cli.common, "connect", return_value=_connect_ok(dev)):
            rc = cli.cmd_show("test-host")
        assert rc == 0
        dev.cli.assert_called_once_with("show interfaces terse", format="json")
        assert '"interface-information"' in capsys.readouterr().out

    def test_cli_exception_returns_1(self, junos_common, mock_args, mock_config):
        mock_args.show_command = "show bgp summary"
        mock_args.show_format = "text"
        dev = MagicMock()
        dev.cli.side_effect = Exception("RPC timeout")
        with patch.object(cli.common, "connect", return_value=_connect_ok(dev)):
            rc = cli.cmd_show("test-host")
        assert rc == 1
        dev.close.assert_called_once()

    def test_dev_close_exception_suppressed(
        self, junos_common, mock_args, mock_config
    ):
        mock_args.show_command = "show version"
        mock_args.show_format = "text"
        dev = MagicMock()
        dev.cli.return_value = "output"
        dev.close.side_effect = Exception("close failed")
        with patch.object(cli.common, "connect", return_value=_connect_ok(dev)):
            assert cli.cmd_show("test-host") == 0


class TestCmdShowFile:
    """cmd_show with -f FILE exercises run_cli_batch."""

    def test_batch_success(self, junos_common, mock_args, mock_config, capsys):
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_args.show_format = "text"
        dev = MagicMock()
        dev.cli.side_effect = ["terse output", "route summary"]
        with (
            patch.object(cli.common, "connect", return_value=_connect_ok(dev)),
            patch.object(
                cli.common,
                "load_commands",
                return_value=["show interfaces terse", "show route summary"],
            ),
        ):
            rc = cli.cmd_show("test-host")
        assert rc == 0
        assert dev.cli.call_count == 2
        out = capsys.readouterr().out
        assert "## show interfaces terse" in out
        assert "## show route summary" in out

    def test_batch_short_circuits(self, junos_common, mock_args, mock_config):
        mock_args.showfile = "commands.txt"
        mock_args.show_command = None
        mock_args.show_format = "text"
        dev = MagicMock()
        dev.cli.side_effect = Exception("RPC timeout")
        with (
            patch.object(cli.common, "connect", return_value=_connect_ok(dev)),
            patch.object(
                cli.common,
                "load_commands",
                return_value=["show a", "show b"],
            ),
        ):
            rc = cli.cmd_show("test-host")
        assert rc == 1
        # Second command not attempted.
        assert dev.cli.call_count == 1
        dev.close.assert_called_once()


class TestLoadCommands:
    """common.load_commands strips blank / comment lines."""

    def test_filters_comments_and_blanks(self, tmp_path):
        f = tmp_path / "commands.txt"
        f.write_text(
            "# comment\n"
            "show version\n"
            "\n"
            "  # indented comment\n"
            "show interfaces terse\n"
            "  show route summary  \n"
        )
        assert common.load_commands(str(f)) == [
            "show version",
            "show interfaces terse",
            "show route summary",
        ]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert common.load_commands(str(f)) == []

    def test_comments_only(self, tmp_path):
        f = tmp_path / "c.txt"
        f.write_text("# a\n# b\n\n")
        assert common.load_commands(str(f)) == []
