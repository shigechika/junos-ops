"""Tests for ``upgrade.rollback`` and the ``rollback`` CLI entry."""

import json
from unittest.mock import MagicMock, patch

import pytest
from jnpr.junos.exception import RpcError, RpcTimeoutError
from lxml import etree

from junos_ops import cli
from junos_ops import upgrade

HOST = "test-host"


def _dev(text):
    dev = MagicMock()
    dev.rpc.request_package_rollback.return_value = etree.fromstring(
        f"<output>{text}</output>"
    )
    return dev


class TestRollbackCore:
    def test_dry_run_does_not_call_rpc(self, mock_args, mock_config):
        mock_args.dry_run = True
        dev = _dev("irrelevant")
        result = upgrade.rollback(HOST, dev)
        dev.rpc.request_package_rollback.assert_not_called()
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["rpc_output"] is None
        assert "dry-run" in result["message"]

    @pytest.mark.parametrize(
        "marker",
        [
            "Deleting bootstrap installer",  # MX
            "NOTICE: The 'pending' set has been removed",  # EX
            "will become active at next reboot",  # SRX3xx
            "Rollback of staged upgrade succeeded",  # SRX1500
            "There is NO image for ROLLBACK",  # SRX4600
        ],
    )
    def test_recognised_success_markers(self, mock_args, mock_config, marker):
        result = upgrade.rollback(HOST, _dev(marker))
        assert result["ok"] is True
        assert result["error"] is None
        assert marker in result["rpc_output"]
        assert "successful" in result["message"]

    def test_rpc_is_called_with_text_format_and_timeout(self, mock_args, mock_config):
        dev = _dev("Deleting bootstrap installer")
        upgrade.rollback(HOST, dev)
        dev.rpc.request_package_rollback.assert_called_once_with(
            {"format": "text"}, dev_timeout=120
        )

    def test_unrecognised_response(self, mock_args, mock_config):
        result = upgrade.rollback(HOST, _dev("something new from a future release"))
        assert result["ok"] is False
        assert result["error"] == "unrecognized_response"
        assert "something new" in result["rpc_output"]
        assert "failed" in result["message"]

    @pytest.mark.parametrize(
        "exc, name",
        [
            (RpcTimeoutError(MagicMock(), "rollback", 120), "RpcTimeoutError"),
            (RpcError(rsp=etree.fromstring("<rpc-error><error-message>x</error-message></rpc-error>")), "RpcError"),
            (ValueError("boom"), "ValueError"),
        ],
    )
    def test_exceptions_map_to_error_names(self, mock_args, mock_config, exc, name):
        dev = MagicMock()
        dev.rpc.request_package_rollback.side_effect = exc
        result = upgrade.rollback(HOST, dev)
        assert result["ok"] is False
        assert result["error"] == name
        assert result["rpc_output"] is None
        assert result["message"]


class TestCmdRollback:
    """cli.cmd_rollback: connection, pending gate, exit codes, JSON payload."""

    def test_connection_failure_returns_1(self, mock_args, mock_config):
        with patch.object(cli, "_open_connection", return_value=None):
            assert cli.cmd_rollback(HOST) == 1

    def test_no_pending_skips_and_returns_0(self, mock_args, mock_config, capsys):
        dev = MagicMock()
        with (
            patch.object(cli, "_open_connection", return_value=dev),
            patch.object(upgrade, "get_pending_version", return_value=None),
            patch.object(upgrade, "rollback") as rb,
        ):
            assert cli.cmd_rollback(HOST) == 0
        rb.assert_not_called()
        dev.close.assert_called_once()
        assert "rollback: skip" in capsys.readouterr().out

    def test_no_pending_json(self, mock_args, mock_config, capsys):
        mock_args.json = True
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(upgrade, "get_pending_version", return_value=None),
        ):
            assert cli.cmd_rollback(HOST) == 0
        row = json.loads(capsys.readouterr().out.strip())
        assert row == {"hostname": HOST, "ok": True, "pending": None, "skipped": True}

    def test_success_text_output_and_exit_0(self, mock_args, mock_config, capsys):
        result = {"ok": True, "dry_run": False, "rpc_output": "x",
                  "message": "rollback: ok", "error": None}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(upgrade, "get_pending_version", return_value="22.4R3-S6.5"),
            patch.object(upgrade, "rollback", return_value=result),
        ):
            assert cli.cmd_rollback(HOST) == 0
        out = capsys.readouterr().out
        assert "pending version is 22.4R3-S6.5" in out
        assert "rollback: ok" in out
        assert "rollback: successful" in out

    def test_failure_returns_1(self, mock_args, mock_config, capsys):
        result = {"ok": False, "dry_run": False, "rpc_output": "x",
                  "message": "rollback: failed", "error": "unrecognized_response"}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(upgrade, "get_pending_version", return_value="22.4R3-S6.5"),
            patch.object(upgrade, "rollback", return_value=result),
        ):
            assert cli.cmd_rollback(HOST) == 1
        assert "rollback: successful" not in capsys.readouterr().out

    def test_json_row_includes_pending_and_result(self, mock_args, mock_config, capsys):
        mock_args.json = True
        result = {"ok": True, "dry_run": False, "rpc_output": "x",
                  "message": "rollback: ok", "error": None}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(upgrade, "get_pending_version", return_value="22.4R3-S6.5"),
            patch.object(upgrade, "rollback", return_value=result),
        ):
            assert cli.cmd_rollback(HOST) == 0
        row = json.loads(capsys.readouterr().out.strip())
        assert row["hostname"] == HOST
        assert row["pending"] == "22.4R3-S6.5"
        assert row["ok"] is True
        assert row["error"] is None

    def test_exception_is_reported_and_returns_1(self, mock_args, mock_config, capsys):
        mock_args.json = True
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(upgrade, "get_pending_version", side_effect=RuntimeError("kaboom")),
        ):
            assert cli.cmd_rollback(HOST) == 1
        row = json.loads(capsys.readouterr().out.strip())
        assert row["ok"] is False
        assert row["error"] == "RuntimeError"
        assert row["error_message"] == "kaboom"
