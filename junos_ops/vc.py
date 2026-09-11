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
import re
import time

from jnpr.junos.exception import ConnectClosedError, RpcError, RpcTimeoutError
from ncclient.operations.errors import TimeoutExpiredError

from junos_ops import common

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


# ---------------------------------------------------------------------------
# Mastership switch (request virtual-chassis routing-engine master switch)
# ---------------------------------------------------------------------------

SWITCH_COMMAND = "request virtual-chassis routing-engine master switch"

# Exceptions that mean "the command left the box and the session went with
# it" — the *expected* outcome of a mastership switch, not a failure.
_SESSION_DROP_EXCEPTIONS = (
    RpcTimeoutError,
    ConnectClosedError,
    TimeoutExpiredError,
    OSError,
)

# Anchored at line start so prose containing the word "error" is not
# mistaken for a rejection.
_REJECTED_RE = re.compile(
    r"^\s*(error:|syntax error|unknown command|permission denied)", re.I | re.M
)


def get_replication_state(dev) -> dict:
    """Collect GRES / NSR replication state (``show task replication``).

    Uses ``get-routing-task-replication-state``, the same RPC PyEZ's
    ``SW`` uses for its GRES check. Verified XML shape:

    .. code-block:: xml

        <task-replication-state>
          <task-gres-state>Enabled</task-gres-state>
          <task-re-mode>Master</task-re-mode>
          <task-protocol-replication>
            <task-protocol-replication-name>OSPF</task-protocol-replication-name>
            <task-protocol-replication-state>Complete</task-protocol-replication-state>
          </task-protocol-replication>
          ...

    :return: dict with keys ``ok`` (bool, False on RPC failure / no XML),
        ``gres`` (str | None), ``re_mode`` (str | None), ``protocols``
        (dict name -> state), ``complete`` (bool: GRES Enabled, RE mode
        Master, at least one protocol listed and every one ``Complete``),
        ``error``, ``error_message``. JSON-native. Does not print.
    """
    result: dict = {
        "ok": False,
        "gres": None,
        "re_mode": None,
        "protocols": {},
        "complete": False,
        "error": None,
        "error_message": None,
    }
    try:
        rsp = dev.rpc.get_routing_task_replication_state(normalize=True)
    except Exception as e:
        result["error"] = type(e).__name__
        result["error_message"] = str(e)
        logger.debug(f"get_replication_state: {result['error']}: {e}")
        return result
    if rsp is None or isinstance(rsp, bool):
        result["error"] = "empty_reply"
        result["error_message"] = "get-routing-task-replication-state returned no XML"
        return result

    result["gres"] = rsp.findtext(".//task-gres-state")
    result["re_mode"] = rsp.findtext(".//task-re-mode")
    for item in rsp.findall(".//task-protocol-replication"):
        name = item.findtext("task-protocol-replication-name")
        state = item.findtext("task-protocol-replication-state")
        if name:
            result["protocols"][name] = state or ""
    result["complete"] = bool(
        result["gres"] == "Enabled"
        and result["re_mode"] == "Master"
        and result["protocols"]
        and all(s == "Complete" for s in result["protocols"].values())
    )
    result["ok"] = True
    return result


def _precheck_problems(status: dict, repl: dict) -> list[str]:
    """Return human-readable reasons the switch must not proceed (empty = go)."""
    problems: list[str] = []
    if not status["ok"]:
        problems.append(
            f"virtual-chassis status unavailable ({status['error']}: {status['error_message']})"
        )
    else:
        roles = [m["role"] for m in status["members"]]
        masters = roles.count("Master")
        backups = roles.count("Backup")
        if masters != 1:
            problems.append(f"expected exactly one Master, found {masters}")
        if backups != 1:
            problems.append(f"expected exactly one Backup, found {backups}")
        for m in status["members"]:
            if m["status"] != "Prsnt":
                problems.append(
                    f"member {m['id']} ({m['role'] or '?'}) is {m['status'] or 'unknown'}, not Prsnt"
                )
    if not repl["ok"]:
        problems.append(
            f"task replication state unavailable ({repl['error']}: {repl['error_message']})"
        )
    else:
        if repl["gres"] != "Enabled":
            problems.append(f"GRES (Stateful Replication) is {repl['gres'] or 'unknown'}, not Enabled")
        if repl["re_mode"] != "Master":
            problems.append(f"RE mode is {repl['re_mode'] or 'unknown'}, not Master")
        if not repl["protocols"]:
            problems.append(
                "no protocol replication listed (NSR not configured?); "
                "the switch would drop routing adjacencies"
            )
        for name, state in repl["protocols"].items():
            if state != "Complete":
                problems.append(f"{name} replication is {state or 'unknown'}, not Complete")
    return problems


def master_switch(hostname: str, dev) -> dict:
    """Run ``request virtual-chassis routing-engine master switch`` once, guarded.

    Pre-checks (all must pass unless ``--force``, which turns them into
    ``warnings``): :func:`get_vc_status` ok with exactly one Master and one
    Backup and every member ``Prsnt``; :func:`get_replication_state` ok
    with GRES Enabled, RE mode Master and every listed protocol
    ``Complete`` (fail closed when nothing is listed). A failed RPC is a
    refusal, never "probably fine".

    The command is sent through ``dev.cli()`` — the NETCONF ``<command>``
    path runs the CLI non-interactively, so there is no ``[yes,no]``
    prompt — and **exactly once**. ``issued`` is set *before* the call:
    any exception afterwards means the command may have left the box.
    Session-drop exceptions are the expected result of a successful
    switch and set ``session_dropped``; only ``RpcError`` and an anchored
    rejection marker in the text reply count as failure.

    :return: dict with keys ``ok``, ``status`` (``dry_run`` / ``refused``
        / ``initiated_unverified`` / ``rejected``), ``dry_run``,
        ``forced``, ``command``, ``issued``, ``session_dropped``,
        ``verified`` (always False here; :func:`wait_for_master` sets it),
        ``before`` (vc status), ``replication``, ``expected_master``
        (the pre-switch Backup id), ``after`` (None here), ``rpc_output``,
        ``warnings``, ``steps``, ``error``, ``error_message``. No
        ``hostname`` key — the display layer injects it. Does not print.
    """
    steps: list[dict] = []
    result: dict = {
        "ok": False,
        "status": "refused",
        "dry_run": common.args.dry_run,
        "forced": bool(common.args.force),
        "command": SWITCH_COMMAND,
        "issued": False,
        "session_dropped": False,
        "verified": False,
        "before": None,
        "replication": None,
        "expected_master": None,
        "after": None,
        "rpc_output": None,
        "warnings": [],
        "steps": steps,
        "error": None,
        "error_message": None,
    }

    status = get_vc_status(dev)
    repl = get_replication_state(dev)
    result["before"] = status
    result["replication"] = repl
    result["expected_master"] = status.get("backup")
    if status["ok"]:
        steps.append({
            "action": "vc_status",
            "message": "\tvirtual-chassis: " + ", ".join(
                f"member {m['id']}={m['role'] or '?'}/{m['status']}" for m in status["members"]
            ),
        })
    if repl["ok"]:
        protos = ", ".join(f"{n}={s}" for n, s in repl["protocols"].items()) or "none"
        steps.append({
            "action": "replication",
            "message": f"\ttask replication: GRES={repl['gres']} RE={repl['re_mode']} {protos}",
        })

    problems = _precheck_problems(status, repl)
    if problems:
        if not common.args.force:
            result["error"] = "precheck_failed"
            result["error_message"] = "; ".join(problems)
            for p in problems:
                steps.append({"action": "error", "message": f"\trefused: {p}"})
            return result
        result["warnings"].extend(problems)
        for p in problems:
            steps.append({"action": "warning", "message": f"\tforce: {p}"})

    if result["expected_master"] is None:
        # Reachable only under --force with a broken Backup count; there
        # is nothing to verify against, so say so up front.
        result["warnings"].append("no single Backup: post-switch verification impossible")

    if common.args.dry_run:
        result["ok"] = True
        result["status"] = "dry_run"
        steps.append({
            "action": "dry_run",
            "message": (
                f"\tdry-run: would run '{SWITCH_COMMAND}' "
                f"(master {status.get('master')} -> {result['expected_master']})"
            ),
        })
        return result

    result["issued"] = True
    try:
        out = dev.cli(SWITCH_COMMAND, warning=False)
    # RpcTimeoutError subclasses RpcError in PyEZ, so the session-drop
    # family must be matched first.
    except _SESSION_DROP_EXCEPTIONS as e:
        result["session_dropped"] = True
        steps.append({
            "action": "switch",
            "message": (
                f"\t'{SWITCH_COMMAND}' issued; session dropped "
                f"({type(e).__name__}) — expected during a mastership switch"
            ),
        })
    except RpcError as e:
        result["error"] = "RpcError"
        result["error_message"] = str(e)
        result["status"] = "rejected"
        steps.append({"action": "error", "message": f"\tswitch rejected: RpcError: {e}"})
        return result
    else:
        text = out if isinstance(out, str) else str(out)
        result["rpc_output"] = text
        if _REJECTED_RE.search(text):
            result["error"] = "command_rejected"
            result["error_message"] = text.strip()
            result["status"] = "rejected"
            steps.append({"action": "error", "message": f"\tswitch rejected: {text.strip()}"})
            return result
        steps.append({
            "action": "switch",
            "message": f"\t'{SWITCH_COMMAND}' issued" + (f": {text.strip()}" if text.strip() else ""),
        })

    result["ok"] = True
    result["status"] = "initiated_unverified"
    return result


def wait_for_master(hostname: str, expected: str, timeout: int, interval: int = 10) -> dict:
    """Reconnect until ``expected`` is the VC Master, or ``timeout`` seconds pass.

    Connection failures and RPC errors while the VC re-forms mean "not
    yet"; they are retried until the deadline. Uses
    :func:`junos_ops.common.connect` with ``gather_facts=False`` and
    closes each probe connection. ``time.sleep`` is called through the
    module attribute so tests can patch it.

    :return: dict with keys ``ok`` (bool), ``after`` (last
        :func:`get_vc_status` dict, or None if never reachable),
        ``replication`` (post-switch :func:`get_replication_state`, only
        when ``ok``), ``elapsed`` (int seconds), ``attempts`` (int),
        ``error`` (``mastership_unchanged`` / ``unreachable`` / None),
        ``error_message``. Does not print.
    """
    result: dict = {
        "ok": False,
        "after": None,
        "replication": None,
        "elapsed": 0,
        "attempts": 0,
        "error": None,
        "error_message": None,
    }
    deadline = time.monotonic() + timeout
    last_problem = None
    while True:
        result["attempts"] += 1
        conn = common.connect(hostname, gather_facts=False)
        if conn["ok"]:
            dev = conn["dev"]
            try:
                status = get_vc_status(dev)
                if status["ok"]:
                    result["after"] = status
                    if status["master"] == str(expected):
                        result["ok"] = True
                        result["replication"] = get_replication_state(dev)
                        return result
                    last_problem = f"master is {status['master']}, expected {expected}"
                else:
                    last_problem = f"{status['error']}: {status['error_message']}"
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
        else:
            last_problem = f"{conn['error']}: {conn['error_message']}"
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(interval, max(0, deadline - now)))
    result["elapsed"] = timeout
    result["error"] = "mastership_unchanged" if result["after"] is not None else "unreachable"
    result["error_message"] = last_problem
    return result
