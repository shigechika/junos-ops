"""Tests for junos_ops.snapshot (the standalone ``snapshot`` subcommand)."""

from unittest.mock import MagicMock, patch

from lxml import etree

from junos_ops import snapshot


def _dev(personality, model="MX5-T"):
    """Build a MagicMock device with the given personality/model facts."""
    dev = MagicMock()
    dev.facts = {"personality": personality, "model": model}
    return dev


class TestCommandSelection:
    """Each supported personality issues the correct request-snapshot RPC."""

    def test_mx_no_args(self, mock_args, capsys):
        dev = _dev("MX")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is True
        dev.rpc.request_snapshot.assert_called_once_with(dev_timeout=300)
        # core must not print
        assert capsys.readouterr().out == ""

    def test_switch_no_args(self, mock_args):
        dev = _dev("SWITCH", model="EX3400-24T")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("sw", dev)
        assert result["ok"] is True
        dev.rpc.request_snapshot.assert_called_once_with(dev_timeout=300)

    def test_srx_branch_slice_alternate(self, mock_args):
        dev = _dev("SRX_BRANCH", model="SRX345")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("fw", dev)
        assert result["ok"] is True
        # positional dict (not kwargs) — mirrors delete_snapshots idiom
        dev.rpc.request_snapshot.assert_called_once_with(
            {"slice": "alternate"}, dev_timeout=300
        )

    def test_timeout_override(self, mock_args):
        mock_args.rpc_timeout = 600
        dev = _dev("MX")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            snapshot.create_snapshot("rt", dev)
        dev.rpc.request_snapshot.assert_called_once_with(dev_timeout=600)


class TestUnsupported:
    """Unverified/unknown platforms are skipped without issuing an RPC."""

    def test_srx_highend_skipped(self, mock_args):
        # SRX4600 confirmed to have no `request system snapshot` command.
        dev = _dev("SRX_HIGHEND", model="SRX4600")
        result = snapshot.create_snapshot("fw", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        assert "unsupported" in result["message"]
        dev.rpc.request_snapshot.assert_not_called()

    def test_srx_midrange_skipped(self, mock_args):
        dev = _dev("SRX_MIDRANGE", model="SRX1500")
        result = snapshot.create_snapshot("fw", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        dev.rpc.request_snapshot.assert_not_called()

    def test_unknown_personality_skipped(self, mock_args):
        dev = _dev("WHATEVER", model="vMX")
        result = snapshot.create_snapshot("x", dev)
        assert result["ok"] is True
        assert result["skipped"] is True
        dev.rpc.request_snapshot.assert_not_called()


class TestDryRun:
    """--dry-run reports the intended command without issuing the RPC."""

    def test_dry_run(self, mock_args):
        mock_args.dry_run = True
        dev = _dev("MX")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert "dry-run" in result["message"]
        assert "request system snapshot" in result["message"]
        dev.rpc.request_snapshot.assert_not_called()


class TestFailureHandling:
    """RPC outcomes are classified into success / no-space / error results."""

    def test_success(self, mock_args):
        dev = _dev("MX")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is True
        assert result["error"] is None
        assert "completed" in result["message"]

    def test_out_of_space_non_fatal(self, mock_args):
        dev = _dev("SWITCH", model="EX2300-24T")
        dev.rpc.request_snapshot.side_effect = OSError(
            "error: Not enough space to create snapshot"
        )
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("sw", dev)
        # Out-of-space is a clean, non-fatal, operator-actionable skip.
        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["error"] == "no_space"
        assert "space" in result["message"].lower()

    def test_rpc_error_is_fatal(self, mock_args):
        from jnpr.junos.exception import RpcError

        dev = _dev("MX")
        dev.rpc.request_snapshot.side_effect = RpcError()
        with patch.object(snapshot, "running_on_alternate_media", return_value=False):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is False
        assert result["error"] == "RpcError"
        assert "snapshot failed" in result["message"]


class TestAlternateMediaGuard:
    """The guard blocks snapshots from a box booted on its alternate media."""

    def test_refuse_when_on_alternate(self, mock_args):
        mock_args.force = False
        dev = _dev("MX")
        with patch.object(snapshot, "running_on_alternate_media", return_value=True):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is False
        assert result["error"] == "running_on_alternate_media"
        assert "--force" in result["message"]
        dev.rpc.request_snapshot.assert_not_called()

    def test_force_overrides_guard(self, mock_args):
        mock_args.force = True
        dev = _dev("MX")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=True):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is True
        dev.rpc.request_snapshot.assert_called_once_with(dev_timeout=300)

    def test_inconclusive_proceeds_with_warning(self, mock_args):
        dev = _dev("MX")
        dev.rpc.request_snapshot.return_value = etree.Element("output")
        with patch.object(snapshot, "running_on_alternate_media", return_value=None):
            result = snapshot.create_snapshot("rt", dev)
        assert result["ok"] is True
        assert any(
            "inconclusive" in step.get("message", "") for step in result["steps"]
        )


class TestRunningOnAlternateMedia:
    """The best-effort detector returns True / False / None defensively."""

    def test_detects_alternate(self, mock_args):
        dev = MagicMock()
        dev.cli.return_value = (
            "NOTICE: System is running on alternate media device (/dev/da1s1a)."
        )
        assert snapshot.running_on_alternate_media(dev) is True

    def test_primary_when_no_marker(self, mock_args):
        dev = MagicMock()
        dev.cli.return_value = "Information for snapshot on internal (primary)"
        assert snapshot.running_on_alternate_media(dev) is False

    def test_none_on_empty(self, mock_args):
        dev = MagicMock()
        dev.cli.return_value = ""
        assert snapshot.running_on_alternate_media(dev) is None

    def test_none_on_exception(self, mock_args):
        dev = MagicMock()
        dev.cli.side_effect = ValueError("boom")
        assert snapshot.running_on_alternate_media(dev) is None
