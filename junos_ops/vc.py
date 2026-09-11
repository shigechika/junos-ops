"""Virtual Chassis helpers.

Read-only status collection for VC-aware operations (member reboot,
mastership switch). Core functions return a result ``dict`` and never
print; RPC failures are captured into the dict (``ok=False``) so callers
can fail closed rather than crash.

XML shape (verified on a two-member QFX5110 VC, Junos 20.x):

.. code-block:: xml

    <virtual-chassis-information>
      <preprovisioned-virtual-chassis-information>
        <virtual-chassis-mode>Enabled</virtual-chassis-mode>
      </preprovisioned-virtual-chassis-information>
      <member-list style="single-slot">
        <member>
          <member-status>Prsnt</member-status>
          <member-id>0</member-id>
          <member-model>qfx5110-48s-4c</member-model>
          <member-priority>129</member-priority>
          <member-role>Master*</member-role>
        </member>
        ...

``member-role`` carries a trailing ``*`` on the member that owns the
management session (``Master*``); it is stripped so callers compare
against plain ``Master`` / ``Backup`` / ``Linecard``.
"""

from logging import getLogger

logger = getLogger(__name__)


def get_vc_status(dev) -> dict:
    """Collect Virtual Chassis membership via ``get-virtual-chassis-information``.

    :return: dict with keys:

        - ``ok`` (bool): False when the RPC failed or returned no
          ``member-list``; callers must treat that as "unknown", never as
          "not a VC".
        - ``mode`` (str | None): ``virtual-chassis-mode`` text
          (``Enabled`` / ``Disabled`` / ``Mixed``) when present.
        - ``members`` (list[dict]): one entry per member with ``id``
          (str), ``role`` (str, ``*`` stripped), ``status`` (str, e.g.
          ``Prsnt`` / ``NotPrsnt``), ``priority`` (str | None), ``model``
          (str | None).
        - ``master`` (str | None): member id whose role is ``Master``
          (None if zero or several claim it).
        - ``backup`` (str | None): member id whose role is ``Backup``
          (None if zero or several).
        - ``error`` (str | None): exception class name when ``ok`` is
          False.
        - ``error_message`` (str | None).

    JSON-native only (no lxml objects). Does not print.
    """
    result: dict = {
        "ok": False,
        "mode": None,
        "members": [],
        "master": None,
        "backup": None,
        "error": None,
        "error_message": None,
    }
    try:
        rsp = dev.rpc.get_virtual_chassis_information(normalize=True)
    except Exception as e:  # RpcError, RpcTimeoutError, ConnectClosedError, ...
        result["error"] = type(e).__name__
        result["error_message"] = str(e)
        logger.debug(f"get_vc_status: {result['error']}: {e}")
        return result

    if rsp is None or isinstance(rsp, bool):
        # PyEZ returns True for an empty <rpc-reply/> (and None on some
        # transports); neither is a VC status, so stay fail-closed.
        result["error"] = "empty_reply"
        result["error_message"] = "get-virtual-chassis-information returned no XML"
        return result

    result["mode"] = rsp.findtext(".//virtual-chassis-mode")
    member_list = rsp.find(".//member-list")
    if member_list is None:
        result["error"] = "no_member_list"
        result["error_message"] = "get-virtual-chassis-information returned no member-list"
        return result

    masters: list[str] = []
    backups: list[str] = []
    for m in member_list.findall("member"):
        member_id = m.findtext("member-id")
        if member_id is None:
            continue
        role = (m.findtext("member-role") or "").rstrip("*")
        entry = {
            "id": member_id,
            "role": role,
            "status": m.findtext("member-status") or "",
            "priority": m.findtext("member-priority"),
            "model": m.findtext("member-model"),
        }
        result["members"].append(entry)
        if role == "Master":
            masters.append(member_id)
        elif role == "Backup":
            backups.append(member_id)

    result["master"] = masters[0] if len(masters) == 1 else None
    result["backup"] = backups[0] if len(backups) == 1 else None
    result["ok"] = True
    return result


def find_member(status: dict, member_id) -> dict | None:
    """Return the member entry with the given id from a :func:`get_vc_status` dict."""
    wanted = str(member_id)
    for m in status.get("members", []):
        if m["id"] == wanted:
            return m
    return None
