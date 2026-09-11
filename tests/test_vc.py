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
