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

    @pytest.mark.parametrize("text", ["error: command not valid on this platform", "  syntax error, expecting <command>"])
    def test_rejection_text(self, mock_args, mock_config, text):
        r = vc.master_switch("h", switch_dev(cli_result=text))
        assert r["ok"] is False and r["status"] == "rejected" and r["error"] == "command_rejected"

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
        clock = iter(range(0, 1000, 5))
        with (
            patch.object(common, "connect", side_effect=seq) as connect,
            patch.object(vc.time, "sleep") as sleep,
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_master("h", "1", timeout=180, interval=10)
        assert r["ok"] is True
        assert r["after"]["master"] == "1"
        assert r["attempts"] == 3
        assert r["replication"]["complete"] is True
        assert connect.call_count == 3
        assert sleep.call_count == 2
        old.close.assert_called_once()
        new.close.assert_called_once()

    def test_timeout_mastership_unchanged(self, mock_args, mock_config):
        old = switch_dev(vc=vc_xml())
        clock = iter([0, 0, 100, 100, 200, 200, 300])
        with (
            patch.object(common, "connect", side_effect=lambda *a, **k: self._conn(old)),
            patch.object(vc.time, "sleep"),
            patch.object(vc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            r = vc.wait_for_master("h", "1", timeout=150, interval=10)
        assert r["ok"] is False
        assert r["error"] == "mastership_unchanged"
        assert "master is 0, expected 1" in r["error_message"]
        assert r["after"]["master"] == "0"

    def test_never_reachable(self, mock_args, mock_config):
        clock = iter([0, 0, 500])
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
