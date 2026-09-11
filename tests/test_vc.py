"""Tests for junos_ops.vc (Virtual Chassis status parsing).

The fixture mirrors real ``get-virtual-chassis-information`` output from a
two-member QFX5110 VC (serials scrubbed): note the trailing ``*`` on the
Master role and the ``Prsnt`` status spelling.
"""

from unittest.mock import MagicMock

import pytest
from jnpr.junos.exception import RpcError
from lxml import etree

from junos_ops import vc

VC_XML = """
<virtual-chassis-information>
  <preprovisioned-virtual-chassis-information>
    <virtual-chassis-id>0000.0000.0001</virtual-chassis-id>
    <virtual-chassis-mode>Enabled</virtual-chassis-mode>
  </preprovisioned-virtual-chassis-information>
  <member-list style="single-slot">
    <member>
      <member-status>Prsnt</member-status>
      <member-id>0</member-id>
      <fpc-slot>(FPC 0)</fpc-slot>
      <member-serial-number>SERIAL0</member-serial-number>
      <member-model>qfx5110-48s-4c</member-model>
      <member-priority>129</member-priority>
      <member-mixed-mode>N</member-mixed-mode>
      <member-route-mode>VC</member-route-mode>
      <member-role>{role0}</member-role>
    </member>
    <member>
      <member-status>{status1}</member-status>
      <member-id>1</member-id>
      <fpc-slot>(FPC 1)</fpc-slot>
      <member-serial-number>SERIAL1</member-serial-number>
      <member-model>qfx5110-48s-4c</member-model>
      <member-priority>129</member-priority>
      <member-mixed-mode>N</member-mixed-mode>
      <member-route-mode>VC</member-route-mode>
      <member-role>{role1}</member-role>
    </member>
    {extra}
  </member-list>
</virtual-chassis-information>
"""


def vc_xml(role0="Master*", role1="Backup", status1="Prsnt", extra=""):
    return etree.fromstring(
        VC_XML.format(role0=role0, role1=role1, status1=status1, extra=extra)
    )


def _rpc_error(message):
    """Build an RpcError whose str() carries the device's message."""
    return RpcError(rsp=etree.fromstring(
        f"<rpc-error><error-message>{message}</error-message></rpc-error>"
    ))


def dev_with(rsp):
    dev = MagicMock()
    if isinstance(rsp, Exception):
        dev.rpc.get_virtual_chassis_information.side_effect = rsp
    else:
        dev.rpc.get_virtual_chassis_information.return_value = rsp
    return dev


class TestGetVcStatus:
    def test_two_member_vc(self):
        st = vc.get_vc_status(dev_with(vc_xml()))
        assert st["ok"] is True
        assert st["mode"] == "Enabled"
        assert st["master"] == "0"
        assert st["backup"] == "1"
        assert [m["id"] for m in st["members"]] == ["0", "1"]
        m0 = st["members"][0]
        assert m0["role"] == "Master"  # trailing '*' stripped
        assert m0["status"] == "Prsnt"
        assert m0["priority"] == "129"
        assert m0["model"] == "qfx5110-48s-4c"
        assert st["error"] is None

    def test_normalize_requested(self):
        dev = dev_with(vc_xml())
        vc.get_vc_status(dev)
        dev.rpc.get_virtual_chassis_information.assert_called_once_with(normalize=True)

    def test_linecard_member_and_master_on_other_slot(self):
        extra = (
            "<member><member-status>Prsnt</member-status><member-id>2</member-id>"
            "<member-role>Linecard</member-role></member>"
        )
        st = vc.get_vc_status(dev_with(vc_xml(role0="Backup", role1="Master*", extra=extra)))
        assert st["master"] == "1"
        assert st["backup"] == "0"
        assert st["members"][2]["role"] == "Linecard"
        assert st["members"][2]["priority"] is None

    def test_not_present_member(self):
        st = vc.get_vc_status(dev_with(vc_xml(role1="", status1="NotPrsnt")))
        assert st["ok"] is True
        assert st["backup"] is None
        assert vc.find_member(st, 1)["status"] == "NotPrsnt"

    def test_two_masters_is_ambiguous(self):
        st = vc.get_vc_status(dev_with(vc_xml(role0="Master*", role1="Master")))
        assert st["ok"] is True
        assert st["master"] is None

    @pytest.mark.parametrize(
        "exc",
        [
            RpcError(rsp=etree.fromstring("<rpc-error><error-message>x</error-message></rpc-error>")),
            TimeoutError("timed out"),
        ],
    )
    def test_rpc_failure_is_not_ok(self, exc):
        st = vc.get_vc_status(dev_with(exc))
        assert st["ok"] is False
        assert st["error"] == type(exc).__name__
        assert st["members"] == []

    def test_missing_member_list(self):
        rsp = etree.fromstring("<virtual-chassis-information/>")
        st = vc.get_vc_status(dev_with(rsp))
        assert st["ok"] is False
        assert st["error"] == "no_member_list"


class TestFindMember:
    def test_int_and_str_ids(self):
        st = vc.get_vc_status(dev_with(vc_xml()))
        assert vc.find_member(st, 1)["id"] == "1"
        assert vc.find_member(st, "1")["id"] == "1"
        assert vc.find_member(st, 5) is None


class TestEmptyReply:
    @pytest.mark.parametrize("rsp", [True, None])
    def test_non_element_reply_is_not_ok(self, rsp):
        st = vc.get_vc_status(dev_with(rsp))
        assert st["ok"] is False
        assert st["error"] == "empty_reply"


# ---------------------------------------------------------------------------
# replication state / master_switch / wait_for_master / cmd_vc_switch
# ---------------------------------------------------------------------------

import itertools  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from unittest.mock import patch  # noqa: E402

from jnpr.junos.exception import ConnectClosedError, RpcTimeoutError  # noqa: E402

from junos_ops import cli, common  # noqa: E402

REPL_XML = """
<task-replication-state>
  <task-gres-state>{gres}</task-gres-state>
  <task-re-mode>{re_mode}</task-re-mode>
  {protocols}
</task-replication-state>
"""


def repl_xml(gres="Enabled", re_mode="Master", protocols=(("OSPF", "Complete"), ("OSPF3", "Complete"))):
    body = "".join(
        f"<task-protocol-replication><task-protocol-replication-name>{n}"
        f"</task-protocol-replication-name><task-protocol-replication-state>{s}"
        f"</task-protocol-replication-state></task-protocol-replication>"
        for n, s in protocols
    )
    return etree.fromstring(REPL_XML.format(gres=gres, re_mode=re_mode, protocols=body))


def switch_dev(vc=None, repl=None, cli_result="Toggle mastership: done"):
    dev = MagicMock()
    dev.rpc.get_virtual_chassis_information.return_value = vc if vc is not None else vc_xml()
    dev.rpc.get_routing_task_replication_state.return_value = repl if repl is not None else repl_xml()
    if isinstance(cli_result, Exception):
        dev.cli.side_effect = cli_result
    else:
        dev.cli.return_value = cli_result
    return dev


class TestGetReplicationState:
    def test_complete(self):
        r = vc.get_replication_state(switch_dev())
        assert r["ok"] is True
        assert r["gres"] == "Enabled" and r["re_mode"] == "Master"
        assert r["protocols"] == {"OSPF": "Complete", "OSPF3": "Complete"}
        assert r["complete"] is True

    def test_not_started_protocol(self):
        r = vc.get_replication_state(
            switch_dev(repl=repl_xml(protocols=(("OSPF", "Complete"), ("BGP", "NotStarted"))))
        )
        assert r["ok"] is True and r["complete"] is False

    def test_no_protocols_is_not_complete(self):
        r = vc.get_replication_state(switch_dev(repl=repl_xml(protocols=())))
        assert r["ok"] is True and r["protocols"] == {} and r["complete"] is False

    def test_gres_disabled(self):
        r = vc.get_replication_state(switch_dev(repl=repl_xml(gres="Disabled")))
        assert r["complete"] is False

    def test_rpc_failure(self):
        dev = MagicMock()
        dev.rpc.get_routing_task_replication_state.side_effect = RpcError()
        r = vc.get_replication_state(dev)
        assert r["ok"] is False and r["error"] == "RpcError" and r["complete"] is False

    def test_empty_reply(self):
        dev = MagicMock()
        dev.rpc.get_routing_task_replication_state.return_value = True
        assert vc.get_replication_state(dev)["error"] == "empty_reply"


class TestMasterSwitch:
    def test_happy_path_is_initiated_unverified(self, mock_args, mock_config):
        dev = switch_dev()
        r = vc.master_switch("h", dev)
        assert r["ok"] is True
        assert r["status"] == "initiated_unverified"
        assert r["issued"] is True and r["session_dropped"] is False and r["verified"] is False
        assert r["expected_master"] == "1"
        assert r["before"]["master"] == "0"
        assert r["rpc_output"] == "Toggle mastership: done"
        dev.cli.assert_called_once_with(vc.SWITCH_COMMAND, warning=False)

    @pytest.mark.parametrize(
        "exc", [RpcTimeoutError(MagicMock(), "cmd", 30), ConnectClosedError(MagicMock()),
                TimeoutError("ssh"), OSError("socket closed")],
    )
    def test_session_drop_is_expected(self, mock_args, mock_config, exc):
        dev = switch_dev(cli_result=exc)
        r = vc.master_switch("h", dev)
        assert r["ok"] is True
        assert r["status"] == "initiated_unverified"
        assert r["issued"] is True and r["session_dropped"] is True
        dev.cli.assert_called_once()  # never retried

    def test_rpc_error_is_rejected(self, mock_args, mock_config):
        dev = switch_dev(cli_result=RpcError())
        r = vc.master_switch("h", dev)
        assert r["ok"] is False and r["status"] == "rejected" and r["error"] == "RpcError"
        assert r["issued"] is True
        dev.cli.assert_called_once()

    @pytest.mark.parametrize("text", ["error: command not valid on this platform", "  permission denied"])
    def test_rejection_text(self, mock_args, mock_config, text):
        dev = switch_dev(cli_result=text)
        r = vc.master_switch("h", dev)
        assert r["ok"] is False and r["status"] == "rejected" and r["error"] == "command_rejected"
        dev.cli.assert_called_once()  # a refusal about this switch is not retried

    def test_prose_error_word_is_not_rejection(self, mock_args, mock_config):
        r = vc.master_switch("h", switch_dev(cli_result="No error occurred; switching"))
        assert r["status"] == "initiated_unverified"

    def test_dry_run_does_not_issue(self, mock_args, mock_config):
        mock_args.dry_run = True
        dev = switch_dev()
        r = vc.master_switch("h", dev)
        assert r["ok"] is True and r["status"] == "dry_run" and r["issued"] is False
        dev.cli.assert_not_called()
        assert any("would run" in s["message"] for s in r["steps"])

    @pytest.mark.parametrize(
        "kwargs, needle",
        [
            ({"vc": vc_xml(role1="Linecard")}, "exactly one Backup"),
            ({"vc": vc_xml(role0="Backup")}, "exactly one Master"),
            ({"vc": vc_xml(role1="Backup", status1="NotPrsnt")}, "not Prsnt"),
            ({"repl": repl_xml(gres="Disabled")}, "not Enabled"),
            ({"repl": repl_xml(re_mode="Backup")}, "not Master"),
            ({"repl": repl_xml(protocols=())}, "no protocol replication"),
            ({"repl": repl_xml(protocols=(("OSPF", "InProgress"),))}, "OSPF replication is InProgress"),
        ],
    )
    def test_precheck_refusals(self, mock_args, mock_config, kwargs, needle):
        dev = switch_dev(**kwargs)
        r = vc.master_switch("h", dev)
        assert r["ok"] is False and r["status"] == "refused"
        assert r["error"] == "precheck_failed"
        assert needle in r["error_message"]
        assert r["issued"] is False
        dev.cli.assert_not_called()

    def test_vc_rpc_failure_refuses(self, mock_args, mock_config):
        dev = switch_dev()
        dev.rpc.get_virtual_chassis_information.side_effect = RpcError()
        r = vc.master_switch("h", dev)
        assert r["status"] == "refused" and "status unavailable" in r["error_message"]
        dev.cli.assert_not_called()

    def test_replication_rpc_failure_refuses(self, mock_args, mock_config):
        dev = switch_dev()
        dev.rpc.get_routing_task_replication_state.side_effect = RpcError()
        r = vc.master_switch("h", dev)
        assert r["status"] == "refused" and "replication state unavailable" in r["error_message"]
        dev.cli.assert_not_called()

    def test_force_turns_problems_into_warnings(self, mock_args, mock_config):
        mock_args.force = True
        dev = switch_dev(repl=repl_xml(protocols=()))
        r = vc.master_switch("h", dev)
        assert r["ok"] is True and r["status"] == "initiated_unverified"
        assert r["forced"] is True
        assert any("no protocol replication" in w for w in r["warnings"])
        dev.cli.assert_called_once()

    def test_linecard_members_are_fine(self, mock_args, mock_config):
        extra = ("<member><member-status>Prsnt</member-status><member-id>2</member-id>"
                 "<member-role>Linecard</member-role></member>")
        r = vc.master_switch("h", switch_dev(vc=vc_xml(extra=extra)))
        assert r["status"] == "initiated_unverified"

    def test_no_hostname_key(self, mock_args, mock_config):
        assert "hostname" not in vc.master_switch("h", switch_dev())


class TestWaitForMaster:
    def _conn(self, dev):
        return {"hostname": "h", "host": "h", "ok": True, "dev": dev, "error": None, "error_message": None}

    def test_confirms_after_reconnects(self, mock_args, mock_config):
        old = switch_dev(vc=vc_xml())                       # master still 0
        new = switch_dev(vc=vc_xml(role0="Backup", role1="Master*"))
        seq = [
            {"ok": False, "dev": None, "error": "ConnectError", "error_message": "refused"},
            self._conn(old),
            self._conn(new),
        ]
        clock = itertools.count(0, 5)
        with (
            patch.object(common, "connect", side_effect=seq) as connect,
            patch.object(vc.time, "sleep") as sleep,
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_master("h", "1", timeout=180, interval=10)
        assert r["ok"] is True
        assert r["after"]["master"] == "1"
        assert r["attempts"] == 3
        assert r["elapsed"] > 0
        # every probe is bounded by the interval / remaining window
        assert all(c.kwargs["auto_probe"] <= 10 for c in connect.call_args_list)
        assert all(c.kwargs["gather_facts"] is False for c in connect.call_args_list)
        assert r["replication"]["complete"] is True
        assert connect.call_count == 3
        assert sleep.call_count == 2
        old.close.assert_called_once()
        new.close.assert_called_once()

    def test_timeout_mastership_unchanged(self, mock_args, mock_config):
        old = switch_dev(vc=vc_xml())
        clock = itertools.count(0, 40)
        with (
            patch.object(common, "connect", side_effect=lambda *a, **k: self._conn(old)),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_master("h", "1", timeout=150, interval=10)
        assert r["ok"] is False
        assert r["error"] == "mastership_unchanged"
        assert r["elapsed"] >= 150
        assert "master is 0, expected 1" in r["error_message"]
        assert r["after"]["master"] == "0"

    def test_never_reachable(self, mock_args, mock_config):
        clock = itertools.count(0, 100)
        with (
            patch.object(common, "connect", return_value={"ok": False, "dev": None, "error": "ConnectError", "error_message": "no route"}),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_master("h", "1", timeout=60)
        assert r["ok"] is False and r["error"] == "unreachable" and r["after"] is None


class TestCmdVcSwitch:
    def _result(self, **over):
        base = {
            "ok": True, "status": "initiated_unverified", "dry_run": False, "forced": False,
            "command": vc.SWITCH_COMMAND, "issued": True, "session_dropped": True,
            "verified": False, "before": {"ok": True, "members": [], "master": "0", "backup": "1"},
            "replication": {"ok": True}, "expected_master": "1", "after": None,
            "rpc_output": None, "warnings": [], "steps": [], "error": None, "error_message": None,
        }
        base.update(over)
        return base

    def test_confirmed_exit_0_json(self, mock_args, mock_config, capsys):
        mock_args.json = True
        after = {"ok": True, "members": [], "master": "1", "backup": "0"}
        waited = {"ok": True, "after": after, "replication": {"ok": True, "complete": True, "protocols": {}},
                  "elapsed": 0, "attempts": 2, "error": None, "error_message": None}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._result()),
            patch.object(vc, "wait_for_master", return_value=waited) as w,
        ):
            assert cli.cmd_vc_switch("h") == 0
        w.assert_called_once_with("h", "1", 180)
        row = json.loads(capsys.readouterr().out.strip())
        assert row["hostname"] == "h"
        assert row["status"] == "confirmed" and row["verified"] is True
        assert row["after"]["master"] == "1"

    def test_verification_failed_exit_1(self, mock_args, mock_config, capsys):
        waited = {"ok": False, "after": {"ok": True, "members": [], "master": "0", "backup": "1"},
                  "replication": None, "elapsed": 180, "attempts": 18,
                  "error": "mastership_unchanged", "error_message": "master is 0, expected 1"}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._result()),
            patch.object(vc, "wait_for_master", return_value=waited),
        ):
            assert cli.cmd_vc_switch("h") == 1
        out = capsys.readouterr().out
        assert "verification FAILED" in out and "mastership_unchanged" in out

    def test_wait_zero_is_unverified_exit_0(self, mock_args, mock_config, capsys):
        mock_args.wait = 0
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._result()),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 0
        w.assert_not_called()
        assert "NOT verified" in capsys.readouterr().out

    def test_refused_exit_1_no_wait(self, mock_args, mock_config, capsys):
        r = self._result(ok=False, status="refused", issued=False, session_dropped=False,
                         error="precheck_failed", error_message="expected exactly one Backup, found 0")
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=r),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 1
        w.assert_not_called()
        assert "REFUSED" in capsys.readouterr().out

    def test_dry_run_exit_0_no_wait(self, mock_args, mock_config, capsys):
        r = self._result(status="dry_run", issued=False, session_dropped=False, dry_run=True)
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=r),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 0
        w.assert_not_called()

    def test_connection_failure(self, mock_args, mock_config):
        with patch.object(cli, "_open_connection", return_value=None):
            assert cli.cmd_vc_switch("h") == 1

    @pytest.mark.parametrize("argv", [["junos-ops", "vc-switch"], ["junos-ops", "vc-switch", "--wait", "-1", "h"]])
    def test_argument_guards(self, argv):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as ei:
                cli._run()
        assert ei.value.code == 2

    @patch("junos_ops.common.run_parallel", return_value={})
    @patch("junos_ops.common.get_targets", return_value=["h"])
    @patch("junos_ops.common.read_config", return_value={"ok": True, "path": "config.ini", "sections": ["h"], "error": None})
    @patch("junos_ops.common.get_default_config", return_value="config.ini")
    def test_dispatch(self, *_):
        with patch.object(sys, "argv", ["junos-ops", "vc-switch", "--wait", "30", "h"]):
            assert cli._run() == 0
        assert common.args.wait == 30
        assert common.args.workers == 1


class TestReviewFollowups:
    @pytest.mark.parametrize(
        "text",
        [
            "Not ready for mastership switch, try after 264 secs.",
            "Toggle mastership between routing engines ? [yes,no] (no)",
            "Mastership switch is not allowed on this platform",
        ],
    )
    def test_device_refusal_wording_is_rejected(self, mock_args, mock_config, text):
        r = vc.master_switch("h", switch_dev(cli_result=text))
        assert r["status"] == "rejected" and r["error"] == "command_rejected"
        assert text in r["error_message"]

    def test_unexpected_exception_after_issue_keeps_result(self, mock_args, mock_config):
        dev = switch_dev(cli_result=ValueError("parser hiccup"))
        r = vc.master_switch("h", dev)
        assert r["ok"] is True and r["status"] == "initiated_unverified"
        assert r["issued"] is True and r["before"]["master"] == "0"
        assert any("ValueError" in w for w in r["warnings"])
        dev.cli.assert_called_once()

    def test_confirmed_but_replication_unavailable_warns(self, mock_args, mock_config, capsys):
        after = {"ok": True, "members": [], "master": "1", "backup": "0"}
        waited = {"ok": True, "after": after,
                  "replication": {"ok": False, "error": "RpcError", "error_message": "x",
                                  "complete": False, "protocols": {}, "gres": None, "re_mode": None},
                  "elapsed": 12, "attempts": 2, "error": None, "error_message": None}
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=TestCmdVcSwitch()._result()),
            patch.object(vc, "wait_for_master", return_value=waited),
        ):
            assert cli.cmd_vc_switch("h") == 0
        out = capsys.readouterr().out
        assert "confirmed" in out and "could not be checked" in out

    def test_connect_forwards_auto_probe(self, mock_config):
        with patch("junos_ops.common.Device") as dev_cls:
            dev_cls.return_value.open.return_value = None
            common.connect("test-host", gather_facts=False, auto_probe=7)
            assert dev_cls.call_args.kwargs["auto_probe"] == 7
            common.connect("test-host")
            assert "auto_probe" not in dev_cls.call_args.kwargs


class TestQfxFallback:
    """#159: QFX VCs reject the virtual-chassis form; fall back to the chassis form."""

    NOT_VALID = "command is not valid on the qfx5110-48s-4c"

    def test_rpc_error_not_valid_falls_back_once(self, mock_args, mock_config):
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error(self.NOT_VALID), "Toggle mastership: done"]
        r = vc.master_switch("h", dev)
        assert r["ok"] is True and r["status"] == "initiated_unverified"
        assert r["command"] == vc.CHASSIS_SWITCH_COMMAND
        assert r["issued"] is True
        assert [c.args[0] for c in dev.cli.call_args_list] == list(vc.SWITCH_COMMANDS)
        assert any(s["action"] == "command_not_valid" for s in r["steps"])

    def test_text_reply_never_falls_back(self, mock_args, mock_config):
        """A text reply means the CLI ran the command: no second form."""
        dev = switch_dev()
        dev.cli.side_effect = ["unknown command: virtual-chassis", "should not be reached"]
        r = vc.master_switch("h", dev)
        assert r["ok"] is False and r["status"] == "rejected"
        assert r["error"] == "command_rejected"
        assert r["command"] == vc.SWITCH_COMMAND
        dev.cli.assert_called_once()

    def test_mixed_success_and_parse_diagnostic_is_not_retried(self, mock_args, mock_config):
        """A reply carrying both a banner and 'syntax error' must not switch twice."""
        dev = switch_dev(cli_result="Toggle mastership: done\nsyntax error, expecting <eol>")
        r = vc.master_switch("h", dev)
        dev.cli.assert_called_once()
        assert r["command"] == vc.SWITCH_COMMAND

    def test_session_drop_on_fallback_is_success(self, mock_args, mock_config):
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error(self.NOT_VALID), RpcTimeoutError(MagicMock(), "cmd", 30)]
        r = vc.master_switch("h", dev)
        assert r["ok"] is True and r["session_dropped"] is True
        assert r["command"] == vc.CHASSIS_SWITCH_COMMAND

    def test_both_forms_invalid_is_rejected(self, mock_args, mock_config):
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error(self.NOT_VALID), _rpc_error(self.NOT_VALID)]
        r = vc.master_switch("h", dev)
        assert r["ok"] is False and r["status"] == "rejected"
        assert r["error"] == "command_not_valid"
        assert r["issued"] is False  # proven not executed
        assert dev.cli.call_count == 2

    def test_other_rpc_error_does_not_fall_back(self, mock_args, mock_config):
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error("permission denied"), "should not be reached"]
        r = vc.master_switch("h", dev)
        assert r["error"] == "RpcError"
        dev.cli.assert_called_once()

    def test_not_ready_does_not_fall_back(self, mock_args, mock_config):
        dev = switch_dev(cli_result="Not ready for mastership switch, try after 264 secs.")
        r = vc.master_switch("h", dev)
        assert r["error"] == "command_rejected"
        dev.cli.assert_called_once()

    def test_first_form_succeeds_without_second_call(self, mock_args, mock_config):
        dev = switch_dev()
        r = vc.master_switch("h", dev)
        assert r["command"] == vc.SWITCH_COMMAND
        dev.cli.assert_called_once_with(vc.SWITCH_COMMAND, warning=False)


class TestRpcOutputEvidence:
    def test_rpc_output_belongs_to_the_issued_command(self, mock_args, mock_config):
        """Only the attempt that produced text sets rpc_output."""
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error(TestQfxFallback.NOT_VALID), "Toggle mastership: done"]
        r = vc.master_switch("h", dev)
        assert r["command"] == vc.CHASSIS_SWITCH_COMMAND
        assert r["rpc_output"] == "Toggle mastership: done"

    def test_no_text_leaks_from_a_refused_first_attempt(self, mock_args, mock_config):
        dev = switch_dev()
        dev.cli.side_effect = [_rpc_error(TestQfxFallback.NOT_VALID),
                               RpcTimeoutError(MagicMock(), "cmd", 30)]
        r = vc.master_switch("h", dev)
        assert r["rpc_output"] is None
        assert r["session_dropped"] is True and r["command"] == vc.CHASSIS_SWITCH_COMMAND


class TestRejectedButIssuedIsVerified:
    """A rejection read off the device's own words is checked against its state."""

    def _rejected(self, **over):
        base = TestCmdVcSwitch()._result(
            ok=False, status="rejected", issued=True, session_dropped=False,
            error="command_rejected",
            error_message="Toggle mastership: done\nsyntax error, expecting <eol>",
        )
        base.update(over)
        return base

    def _waited(self, ok, master):
        return {
            "ok": ok,
            "after": {"ok": True, "members": [], "master": master, "backup": "0"},
            "replication": {"ok": True, "complete": True, "protocols": {}, "gres": "Enabled",
                            "re_mode": "Master", "error": None, "error_message": None},
            "elapsed": 20, "attempts": 2,
            "error": None if ok else "mastership_unchanged",
            "error_message": None if ok else "master is 0, expected 1",
        }

    def test_mastership_moved_upgrades_to_confirmed(self, mock_args, mock_config, capsys):
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._rejected()),
            patch.object(vc, "wait_for_master", return_value=self._waited(True, "1")) as w,
        ):
            assert cli.cmd_vc_switch("h") == 0
        w.assert_called_once_with("h", "1", 180)
        out = capsys.readouterr().out
        assert "confirmed" in out
        assert "may not be what moved it" in out
        assert "device reply (read as a rejection)" in out

    def test_causality_is_not_claimed(self, mock_args, mock_config):
        """A concurrent/manual switch looks identical: keep the evidence."""
        r = self._rejected()
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=r),
            patch.object(vc, "wait_for_master", return_value=self._waited(True, "1")),
        ):
            cli.cmd_vc_switch("h")
        assert r["status"] == "confirmed" and r["ok"] is True and r["error"] is None
        assert r["rejected_reply"].startswith("Toggle mastership: done")
        assert any("may not be what moved it" in w for w in r["warnings"])

    def test_mastership_unchanged_keeps_rejection(self, mock_args, mock_config, capsys):
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._rejected()),
            patch.object(vc, "wait_for_master", return_value=self._waited(False, "0")),
        ):
            assert cli.cmd_vc_switch("h") == 1
        out = capsys.readouterr().out
        assert "REJECTED" in out
        assert "the rejection is real" in out

    def test_not_issued_rejection_is_not_verified(self, mock_args, mock_config):
        """Pre-check refusal / both forms invalid: nothing ran, nothing to verify."""
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch",
                         return_value=self._rejected(issued=False, error="command_not_valid")),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 1
        w.assert_not_called()

    def test_wait_zero_does_not_verify(self, mock_args, mock_config):
        mock_args.wait = 0
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._rejected()),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 1
        w.assert_not_called()

    def test_rejected_without_expected_master_stays_rejected(self, mock_args, mock_config):
        with (
            patch.object(cli, "_open_connection", return_value=MagicMock()),
            patch.object(vc, "master_switch", return_value=self._rejected(expected_master=None)),
            patch.object(vc, "wait_for_master") as w,
        ):
            assert cli.cmd_vc_switch("h") == 1
        w.assert_not_called()


# ---------------------------------------------------------------------------
# member reboot verification (#161)
# ---------------------------------------------------------------------------

FPC_XML = """
<fpc-information style="brief">
  <fpc><slot>0</slot><state>{s0}</state></fpc>
  <fpc><slot>1</slot><state>{s1}</state></fpc>
  <fpc><slot>2</slot><state>Empty</state></fpc>
</fpc-information>
"""

IFACE_XML = """
<interface-information style="terse">
  <physical-interface><name>
ge-0/0/40
</name><admin-status>
up
</admin-status><oper-status>
{o40}
</oper-status></physical-interface>
  <physical-interface><name>
xe-0/0/47
</name><admin-status>
up
</admin-status><oper-status>
{o47}
</oper-status></physical-interface>
</interface-information>
"""


UPTIME_XML = """
<multi-routing-engine-results>
  <multi-routing-engine-item><re-name>localre</re-name><system-uptime-information>
    <system-booted-time><date-time>{b1}</date-time></system-booted-time>
  </system-uptime-information></multi-routing-engine-item>
  <multi-routing-engine-item><re-name>fpc0</re-name><system-uptime-information>
    <system-booted-time><date-time>{b0}</date-time></system-booted-time>
  </system-uptime-information></multi-routing-engine-item>
</multi-routing-engine-results>
"""

BOOT_BEFORE = "2026-06-17 04:11:55 JST"
BOOT_AFTER = "2026-09-11 23:47:42 JST"


def member_dev(vc_rsp=None, s0="Online", s1="Online", o40="up", o47="up", booted=BOOT_AFTER):
    dev = MagicMock()
    dev.rpc.get_virtual_chassis_information.return_value = vc_rsp if vc_rsp is not None else vc_xml()
    dev.rpc.get_fpc_information.return_value = etree.fromstring(FPC_XML.format(s0=s0, s1=s1))
    dev.rpc.get_interface_information.return_value = etree.fromstring(
        IFACE_XML.format(o40=o40, o47=o47)
    )
    dev.rpc.get_system_uptime_information.return_value = etree.fromstring(
        UPTIME_XML.format(b0=booted, b1="2026-06-17 04:11:43 JST")
    )
    return dev


class TestGetFpcState:
    def test_slot_state(self):
        assert vc.get_fpc_state(member_dev(), 0) == "Online"
        assert vc.get_fpc_state(member_dev(s0="Present"), "0") == "Present"
        assert vc.get_fpc_state(member_dev(), 2) == "Empty"

    def test_unknown_slot_and_failures(self):
        assert vc.get_fpc_state(member_dev(), 7) is None
        dev = member_dev()
        dev.rpc.get_fpc_information.side_effect = RpcError()
        assert vc.get_fpc_state(dev, 0) is None
        dev2 = member_dev()
        dev2.rpc.get_fpc_information.return_value = True
        assert vc.get_fpc_state(dev2, 0) is None


class TestGetInterfaceStates:
    def test_states_are_stripped_and_combined(self):
        st = vc.get_interface_states(member_dev(), ["ge-0/0/40", "xe-0/0/47"])
        assert st == {"ge-0/0/40": "up/up", "xe-0/0/47": "up/up"}

    def test_down_and_unknown(self):
        st = vc.get_interface_states(member_dev(o47="down"), ["xe-0/0/47", "ge-0/0/99"])
        assert st["xe-0/0/47"] == "up/down"
        assert st["ge-0/0/99"] is None

    def test_empty_request_and_rpc_failure(self):
        assert vc.get_interface_states(member_dev(), []) == {}
        dev = member_dev()
        dev.rpc.get_interface_information.side_effect = RpcError()
        assert vc.get_interface_states(dev, ["ge-0/0/40"]) == {"ge-0/0/40": None}


class TestWaitForMember:
    def _conn(self, dev):
        return {"hostname": "h", "host": "h", "ok": True, "dev": dev,
                "error": None, "error_message": None}

    def _fail(self):
        return {"ok": False, "dev": None, "error": "ConnectTimeoutError",
                "error_message": "timed out"}

    def _run(self, seq, **kw):
        clock = itertools.count(0, 5)
        kw.setdefault("booted_before", BOOT_BEFORE)
        with (
            patch.object(common, "connect", side_effect=seq) as connect,
            patch.object(vc.time, "sleep") as sleep,
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_member("h", kw.pop("member", 0), kw.pop("timeout", 600), **kw)
        return r, connect, sleep

    def test_comes_back_after_unreachable_window(self, mock_args, mock_config):
        back = member_dev()
        seq = [self._fail(), self._fail(), self._conn(back)]
        r, connect, sleep = self._run(seq)
        assert r["ok"] is True
        assert r["fpc_state"] == "Online"
        assert r["after"]["master"] == "0"
        assert r["attempts"] == 3 and r["elapsed"] > 0
        assert sleep.call_count == 2
        assert all(c.kwargs["gather_facts"] is False for c in connect.call_args_list)
        back.close.assert_called_once()

    def test_present_but_fpc_not_online_is_not_back(self, mock_args, mock_config):
        dev = member_dev(s0="Present")
        r, _, _ = self._run([self._conn(dev)] * 3, timeout=10)
        assert r["ok"] is False and r["error"] == "member_not_back"
        assert "FPC 0 is Present, not Online" in r["error_message"]
        assert r["fpc_state"] == "Present"

    def test_expect_up_gates_success(self, mock_args, mock_config):
        down = member_dev(o47="down")
        up = member_dev()
        r, _, _ = self._run(
            [self._conn(down), self._conn(up)], expect_up=["ge-0/0/40", "xe-0/0/47"]
        )
        assert r["ok"] is True
        assert r["interfaces"] == {"ge-0/0/40": "up/up", "xe-0/0/47": "up/up"}

    def test_expect_up_timeout_reports_the_port(self, mock_args, mock_config):
        r, _, _ = self._run(
            [self._conn(member_dev(o47="down"))] * 3, timeout=10, expect_up=["xe-0/0/47"]
        )
        assert r["ok"] is False and r["error"] == "member_not_back"
        assert "xe-0/0/47=up/down" in r["error_message"]

    def test_member_absent_from_vc(self, mock_args, mock_config):
        r, _, _ = self._run([self._conn(member_dev())] * 2, member=7, timeout=10)
        assert r["ok"] is False
        assert "member 7 is absent" in r["error_message"]

    def test_never_reachable(self, mock_args, mock_config):
        r, _, _ = self._run([self._fail()] * 3, timeout=10)
        assert r["ok"] is False and r["error"] == "unreachable"
        assert r["after"] is None


class TestWaitForMemberRebootEvidence:
    """#164 review: 'healthy right now' is not evidence that the reboot happened."""

    def _conn(self, dev):
        return {"hostname": "h", "host": "h", "ok": True, "dev": dev,
                "error": None, "error_message": None}

    def _fail(self):
        return {"ok": False, "dev": None, "error": "ConnectTimeoutError",
                "error_message": "timed out"}

    def _run(self, seq, **kw):
        """seq may be a list (exact sequence) or a single conn dict (repeated)."""
        clock = itertools.count(0, 25)
        side = seq if isinstance(seq, list) else (lambda *a, **k: seq)
        with (
            patch.object(common, "connect", side_effect=side),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            return vc.wait_for_member("h", kw.pop("member", 0), kw.pop("timeout", 60), **kw)

    def test_unchanged_boot_time_is_not_success(self, mock_args, mock_config):
        """The member is still up from before the reboot RPC took effect."""
        still_old = member_dev(booted=BOOT_BEFORE)
        r = self._run(self._conn(still_old), booted_before=BOOT_BEFORE)
        assert r["ok"] is False and r["error"] == "member_not_back"
        assert "has not rebooted yet" in r["error_message"]

    def test_changed_boot_time_is_success_on_the_first_probe(self, mock_args, mock_config):
        r = self._run([self._conn(member_dev(booted=BOOT_AFTER))], booted_before=BOOT_BEFORE)
        assert r["ok"] is True and r["rebooted"] is True
        assert r["booted"] == BOOT_AFTER

    def test_unreadable_boot_time_keeps_waiting(self, mock_args, mock_config):
        dev = member_dev()
        dev.rpc.get_system_uptime_information.side_effect = RpcError()
        r = self._run(self._conn(dev), booted_before=BOOT_BEFORE)
        assert r["ok"] is False
        assert "cannot read the member's boot time" in r["error_message"]

    def test_without_baseline_requires_an_observed_transition(self, mock_args, mock_config):
        healthy = member_dev()
        r = self._run(self._conn(healthy), booted_before=None)
        assert r["ok"] is False
        assert "has not gone down yet" in r["error_message"]

    def test_without_baseline_a_transition_unblocks_it(self, mock_args, mock_config):
        r = self._run([self._fail(), self._conn(member_dev())], booted_before=None)
        assert r["ok"] is True and r["rebooted"] is True

    def test_master_member_uses_localre_uptime(self, mock_args, mock_config):
        dev = member_dev()
        assert vc.get_member_boot_time(dev, 1, master="1") == "2026-06-17 04:11:43 JST"
        assert vc.get_member_boot_time(dev, 0) == BOOT_AFTER
        assert vc.get_member_boot_time(dev, 5) is None

    def test_single_re_uptime_reply(self, mock_args, mock_config):
        dev = member_dev()
        dev.rpc.get_system_uptime_information.return_value = etree.fromstring(
            "<system-uptime-information><system-booted-time>"
            f"<date-time>{BOOT_AFTER}</date-time></system-booted-time></system-uptime-information>"
        )
        assert vc.get_member_boot_time(dev, 0) == BOOT_AFTER


class TestLogicalInterfaceExpectations:
    def test_logical_names_match(self):
        xml = (
            "<interface-information><physical-interface><name>ae0</name>"
            "<admin-status>up</admin-status><oper-status>up</oper-status>"
            "<logical-interface><name>ae0.0</name><admin-status>up</admin-status>"
            "<oper-status>down</oper-status></logical-interface></physical-interface>"
            "</interface-information>"
        )
        dev = MagicMock()
        dev.rpc.get_interface_information.return_value = etree.fromstring(xml)
        st = vc.get_interface_states(dev, ["ae0", "ae0.0"])
        assert st == {"ae0": "up/up", "ae0.0": "up/down"}


class TestPollBoundsRpcs:
    def test_device_timeout_is_capped_by_the_window(self, mock_args, mock_config):
        dev = member_dev()
        conn = {"hostname": "h", "host": "h", "ok": True, "dev": dev,
                "error": None, "error_message": None}
        clock = itertools.count(0, 5)
        with (
            patch.object(common, "connect", return_value=conn),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            vc.wait_for_member("h", 0, 30, booted_before=BOOT_BEFORE)
        assert dev.timeout <= 30 and dev.timeout >= 5


class TestBootTimeComparison:
    def test_newer_is_evidence(self):
        assert vc.boot_time_is_newer(BOOT_AFTER, BOOT_BEFORE) is True

    def test_same_instant_reformatted_is_not_evidence(self):
        """A re-zoned/reformatted rendering of the same boot must not pass."""
        assert vc.boot_time_is_newer("2026-06-17 04:11:55 UTC", BOOT_BEFORE) is False
        assert vc.boot_time_is_newer(BOOT_BEFORE, BOOT_BEFORE) is False

    def test_older_is_not_evidence(self):
        assert vc.boot_time_is_newer(BOOT_BEFORE, BOOT_AFTER) is False

    def test_unparseable_falls_back_to_inequality(self):
        assert vc.boot_time_is_newer("boot A", "boot B") is True
        assert vc.boot_time_is_newer("boot A", "boot A") is False

    def test_missing_values(self):
        assert vc.boot_time_is_newer(None, BOOT_BEFORE) is False
        assert vc.boot_time_is_newer(BOOT_AFTER, None) is False


class TestWaitForMemberEdgeCases:
    def _conn(self, dev):
        return {"hostname": "h", "host": "h", "ok": True, "dev": dev,
                "error": None, "error_message": None}

    def _run(self, seq, **kw):
        clock = itertools.count(0, 25)
        side = seq if isinstance(seq, list) else (lambda *a, **k: seq)
        with (
            patch.object(common, "connect", side_effect=side),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            return vc.wait_for_member("h", kw.pop("member", 0), kw.pop("timeout", 60), **kw)

    def test_mastership_moving_to_the_member_still_reads_its_uptime(self, mock_args, mock_config):
        """The member became Master while rebooting: its block is now localre."""
        dev = member_dev(vc_rsp=vc_xml(role0="Master*", role1="Backup"))
        # Session lands on the (new) master, so member 0 is "localre" and
        # there is no fpc0 block at all — the realistic shape.
        dev.rpc.get_system_uptime_information.return_value = etree.fromstring(
            "<multi-routing-engine-results>"
            "<multi-routing-engine-item><re-name>localre</re-name><system-uptime-information>"
            f"<system-booted-time><date-time>{BOOT_AFTER}</date-time></system-booted-time>"
            "</system-uptime-information></multi-routing-engine-item>"
            "<multi-routing-engine-item><re-name>fpc1</re-name><system-uptime-information>"
            "<system-booted-time><date-time>2026-06-17 04:11:43 JST</date-time></system-booted-time>"
            "</system-uptime-information></multi-routing-engine-item>"
            "</multi-routing-engine-results>"
        )
        r = self._run(self._conn(dev), member=0, booted_before=BOOT_BEFORE, master="1")
        assert r["ok"] is True
        assert r["booted"] == BOOT_AFTER  # read from localre, per the *current* master

    def test_status_rpc_failure_is_not_a_transition(self, mock_args, mock_config):
        """No baseline: a flaky RPC must not stand in for the member going down."""
        broken = member_dev()
        broken.rpc.get_virtual_chassis_information.side_effect = [
            RpcError(), etree.fromstring(etree.tostring(vc_xml())),
        ]
        clock = itertools.count(0, 2)
        with (
            patch.object(common, "connect", return_value=self._conn(broken)),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_member("h", 0, 10, booted_before=None)
        assert r["ok"] is False
        assert "has not gone down yet" in r["error_message"]

    def test_rpc_timeout_is_bounded_by_the_interval(self, mock_args, mock_config):
        dev = member_dev(booted=BOOT_BEFORE)
        clock = itertools.count(0, 25)
        with (
            patch.object(common, "connect", return_value=self._conn(dev)),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            vc.wait_for_member("h", 0, 600, interval=15, booted_before=BOOT_BEFORE)
        assert dev.timeout == 15
