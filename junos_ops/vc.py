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
import datetime
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

# EX Virtual Chassis form. QFX VCs reject it ("command is not valid on the
# qfx5110-48s-4c") and want the chassis form instead; ``no-confirm`` keeps
# the CLI from prompting, which the NETCONF <command> path cannot answer.
SWITCH_COMMAND = "request virtual-chassis routing-engine master switch"
CHASSIS_SWITCH_COMMAND = "request chassis routing-engine master switch no-confirm"
SWITCH_COMMANDS = (SWITCH_COMMAND, CHASSIS_SWITCH_COMMAND)

# Exceptions that mean "the command left the box and the session went with
# it" — the *expected* outcome of a mastership switch, not a failure.
_SESSION_DROP_EXCEPTIONS = (
    RpcTimeoutError,
    ConnectClosedError,
    TimeoutExpiredError,
    OSError,
)

# Anchored at line start so prose containing the word "error" is not
# mistaken for a rejection. Besides the CLI's own error forms this covers
# chassisd refusing the switch ("Not ready for mastership switch, try
# after N secs.") and the <command> path echoing an unanswered
# confirmation ("... ? [yes,no] (no)") — in both cases nothing happened.
# Proof that mgd refused the command before dispatching it: the RPC layer
# raised instead of returning output. Only an RpcError matching this is
# allowed to advance to the next candidate form — see the "at most once"
# note in :func:`master_switch`. A refusal like "Not ready for mastership
# switch" or an echoed [yes,no] prompt is about *this* switch and must
# never trigger a second command.
_NOT_VALID_RE = re.compile(
    r"\b(command is not valid|unknown command|syntax error)\b", re.I
)


def _is_not_valid_error(exc: RpcError) -> bool:
    """True when an RpcError is mgd refusing to parse/accept the command.

    Matched against PyEZ's ``.message`` (the device's own
    ``<error-message>``) rather than ``str(exc)``, which wraps it in
    ``RpcError(severity: …, message: …)``. Applied only to a raised
    error — never to returned text — so "no output" already implies the
    command was not dispatched.
    """
    return bool(_NOT_VALID_RE.search(getattr(exc, "message", "") or str(exc)))

_REJECTED_RE = re.compile(
    r"^\s*(error:|syntax error|unknown command|permission denied)"
    r"|\bnot (ready|allowed|possible|supported)\b|\[yes,no\]",
    re.I | re.M,
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
    prompt. Each form is sent **at most once**: the EX Virtual Chassis
    form first and, only when the RPC layer *raised* an
    :class:`RpcError` whose message is a parse/platform rejection
    ("command is not valid on the qfx5110-…", "unknown command", "syntax
    error"), the QFX chassis form. mgd rejects those before dispatching
    the command, so nothing ran. Any reply that comes back as *text* —
    including one that looks like an error — ends the attempt: the CLI
    processed the command, and a second destructive form must not be
    sent. ``issued`` is set *before* the call:
    any exception afterwards means the command may have left the box.
    Session-drop exceptions are the expected result of a successful
    switch and set ``session_dropped``; only ``RpcError`` and an anchored
    rejection marker in the text reply count as failure.

    :return: dict with keys ``ok``, ``status`` (``dry_run`` / ``refused``
        / ``initiated_unverified`` / ``rejected``), ``dry_run``,
        ``forced``, ``command`` (the form actually issued), ``issued``,
        ``session_dropped``,
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
                f"(master {status.get('master')} -> {result['expected_master']}), "
                f"falling back to '{CHASSIS_SWITCH_COMMAND}' if the platform "
                "rejects it as not valid"
            ),
        })
        return result

    for attempt, command in enumerate(SWITCH_COMMANDS):
        result["command"] = command
        result["issued"] = True
        not_valid: str | None = None
        try:
            out = dev.cli(command, warning=False)
        # RpcTimeoutError subclasses RpcError in PyEZ, so the session-drop
        # family must be matched first.
        except _SESSION_DROP_EXCEPTIONS as e:
            result["session_dropped"] = True
            steps.append({
                "action": "switch",
                "message": (
                    f"\t'{command}' issued; session dropped "
                    f"({type(e).__name__}) — expected during a mastership switch"
                ),
            })
        except RpcError as e:
            if _is_not_valid_error(e):
                not_valid = getattr(e, "message", "") or str(e)
            else:
                result["error"] = "RpcError"
                result["error_message"] = str(e)
                result["status"] = "rejected"
                steps.append({"action": "error", "message": f"\tswitch rejected: RpcError: {e}"})
                return result
        except Exception as e:
            # Anything else after issued=True: the command may be in flight.
            # Keep the result (and let --wait verify) instead of letting the
            # worker-level handler discard it.
            result["warnings"].append(
                f"unexpected {type(e).__name__} after issuing the switch: {e}"
            )
            steps.append({
                "action": "switch",
                "message": (
                    f"\t'{command}' issued; unexpected {type(e).__name__}: {e} "
                    "— treating as possibly in flight"
                ),
            })
        else:
            # A text reply means the CLI received and processed the
            # command. Whatever it says, a second (destructive) form is
            # never sent on this path: a reply that mixed a success
            # banner with a parse diagnostic would otherwise switch
            # mastership twice.
            text = out if isinstance(out, str) else str(out)
            result["rpc_output"] = text
            if _REJECTED_RE.search(text):
                result["error"] = "command_rejected"
                result["error_message"] = text.strip()
                result["status"] = "rejected"
                steps.append({"action": "error", "message": f"\tswitch rejected: {text.strip()}"})
                return result
            else:
                steps.append({
                    "action": "switch",
                    "message": f"\t'{command}' issued" + (f": {text.strip()}" if text.strip() else ""),
                })

        if not_valid is None:
            result["ok"] = True
            result["status"] = "initiated_unverified"
            return result

        # The device could not parse the command, which proves it did not
        # run: lower ``issued`` again and try the next platform's form.
        result["issued"] = False
        remaining = attempt + 1 < len(SWITCH_COMMANDS)
        steps.append({
            "action": "command_not_valid",
            "message": (
                f"\t'{command}' not valid on this platform ({not_valid})"
                + (f"; trying '{SWITCH_COMMANDS[attempt + 1]}'" if remaining else "")
            ),
        })
        if not remaining:
            result["error"] = "command_not_valid"
            result["error_message"] = not_valid
            result["status"] = "rejected"
            return result

    return result  # pragma: no cover - loop always returns


def get_fpc_state(dev, slot) -> str | None:
    """Return the ``state`` of one FPC slot from ``get-fpc-information``.

    ``show chassis fpc`` reports every slot (``Online`` / ``Empty`` / …);
    on a VC the member id is the FPC slot. Returns None when the RPC
    fails or the slot is absent — callers treat that as "unknown".
    """
    try:
        rsp = dev.rpc.get_fpc_information(normalize=True)
    except Exception as e:
        logger.debug(f"get_fpc_state: {type(e).__name__}: {e}")
        return None
    if rsp is None or isinstance(rsp, bool):
        return None
    for fpc in rsp.findall(".//fpc"):
        if (fpc.findtext("slot") or "").strip() == str(slot):
            return (fpc.findtext("state") or "").strip() or None
    return None


def get_interface_states(dev, names) -> dict:
    """Return ``{name: "up/up" | "down/up" | … | None}`` for the given interfaces.

    One ``get-interface-information(terse=True)`` call, matched against
    both ``physical-interface/name`` and ``logical-interface/name`` so
    ``ge-0/0/40`` and ``ae0.0`` are equally usable. Values are
    ``"<admin>/<oper>"``; a name the device did not report maps to None
    ("unknown", never "up"). Text nodes are whitespace-wrapped in the
    terse reply, hence the ``strip()`` on every field.
    """
    wanted = [n.strip() for n in names if n and n.strip()]
    states: dict = {n: None for n in wanted}
    if not wanted:
        return states
    try:
        rsp = dev.rpc.get_interface_information(terse=True, normalize=True)
    except Exception as e:
        logger.debug(f"get_interface_states: {type(e).__name__}: {e}")
        return states
    if rsp is None or isinstance(rsp, bool):
        return states
    for node in rsp.iter("physical-interface", "logical-interface"):
        name = (node.findtext("name") or "").strip()
        if name in states:
            admin = (node.findtext("admin-status") or "").strip()
            oper = (node.findtext("oper-status") or "").strip()
            states[name] = f"{admin}/{oper}"
    return states


def boot_time_is_newer(booted: str | None, baseline: str | None) -> bool:
    """True when ``booted`` is evidence of a reboot since ``baseline``.

    Both values come from ``show system uptime``. When both parse as
    ``YYYY-MM-DD HH:MM:SS`` the comparison is on the instant, so a
    reformatted or re-zoned rendering of the *same* boot is not mistaken
    for a reboot; otherwise it falls back to string inequality. A reboot
    always yields a later timestamp.
    """
    if not booted or not baseline:
        return False
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.datetime.strptime(booted[:19], fmt) > datetime.datetime.strptime(
            baseline[:19], fmt
        )
    except ValueError:
        return booted.strip() != baseline.strip()


def get_member_boot_time(dev, member, master=None) -> str | None:
    """Return the member's ``System booted`` timestamp text, or None.

    On a VC ``show system uptime`` reports one block per RE: ``fpcN``
    for every member except the one the session is on, which appears as
    ``localre`` (accepted only when ``member`` is the current
    ``master``). The raw device-local string is returned — callers only
    ever compare it for equality, so it is never parsed.
    """
    try:
        up = dev.rpc.get_system_uptime_information(normalize=True)
    except Exception as e:
        logger.debug(f"get_member_boot_time: {type(e).__name__}: {e}")
        return None
    if up is None or isinstance(up, bool):
        return None
    items = up.findall(".//multi-routing-engine-item")
    node = None
    if items:
        by_name = {(it.findtext("re-name") or "").strip(): it for it in items}
        node = by_name.get(f"fpc{member}")
        if node is None and master is not None and str(member) == str(master):
            node = by_name.get("localre")
        if node is None:
            return None
    else:
        node = up
    el = node.find(".//system-booted-time/date-time")
    text = (el.text or "").strip() if el is not None else ""
    return text or None


def _poll_device(hostname: str, timeout: int, interval: int, check, on_unreachable=None) -> dict:
    """Reconnect to ``hostname`` until ``check(dev)`` reports done.

    ``check(dev)`` returns ``(done: bool, snapshot: dict, problem: str |
    None)``. Connection failures and RPC errors mean "not yet" and are
    retried until the deadline, which is checked between probes — a
    probe already in flight is bounded by the interval, so ``timeout``
    is a budget rather than a hard kill; ``on_unreachable()`` (optional) is
    called for each failed connect, which is how a caller learns the
    device went away between probes; each probe is bounded by what is left of
    the window so an unreachable device cannot overshoot ``timeout``.
    ``time.sleep`` / ``time.monotonic`` go through the module so tests
    can patch them.

    :return: dict with ``ok``, ``last`` (last snapshot, or None if the
        device was never reachable), ``elapsed``, ``attempts``,
        ``error_message`` (the last problem seen).
    """
    result: dict = {
        "ok": False, "last": None, "elapsed": 0, "attempts": 0, "error_message": None,
    }
    start = time.monotonic()
    deadline = start + timeout
    last_problem = None
    while True:
        result["attempts"] += 1
        remaining = max(1, int(deadline - time.monotonic()))
        conn = common.connect(
            hostname, gather_facts=False, auto_probe=min(interval, remaining)
        )
        if conn["ok"]:
            dev = conn["dev"]
            try:
                # --wait is a budget, not just a connect timeout: cap
                # each RPC too, or a half-responsive device runs past it.
                # Bounded by the probe interval rather than the whole
                # window so one slow RPC cannot eat the entire budget.
                try:
                    dev.timeout = max(
                        5, min(interval, int(deadline - time.monotonic()))
                    )
                except Exception:  # pragma: no cover - Device always allows it
                    pass
                done, snapshot, problem = check(dev)
                if snapshot is not None:
                    result["last"] = snapshot
                if done:
                    result["ok"] = True
                    result["elapsed"] = int(time.monotonic() - start)
                    return result
                last_problem = problem
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
        else:
            if on_unreachable is not None:
                on_unreachable()
            last_problem = f"{conn['error']}: {conn['error_message']}"
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(interval, max(0, deadline - now)))
    result["elapsed"] = int(time.monotonic() - start)
    result["error_message"] = last_problem
    return result


def wait_for_member(
    hostname: str, member, timeout: int, interval: int = 15, expect_up=(),
    booted_before: str | None = None, master=None,
) -> dict:
    """Reconnect until VC ``member`` is back, or ``timeout`` seconds pass.

    "Back" means: the member appears in ``show virtual-chassis status``
    as ``Prsnt`` with a role, its FPC slot is ``Online``, and every
    interface in ``expect_up`` is ``up/up``. A member that is ``Prsnt``
    but whose PFE is not ready yet would otherwise look recovered while
    traffic hashed to it is black-holed, which is why the FPC state and
    the caller's ports are part of the condition.

    Crucially the member also has to be *observed rebooting* first: a
    reboot RPC returns before the member goes down, so the very first
    probe would otherwise find the pre-reboot member healthy and report
    success. ``booted_before`` (read by the caller before issuing the
    reboot) settles it — the boot timestamp must have changed. Without
    it the fallback is a transition: some probe must have found the
    device unreachable, or the member absent / not ``Prsnt`` / its FPC
    not ``Online``. (A VC-status RPC failure does not count — it says
    nothing about the member.)

    Note the whole VC can be unreachable while one member reboots (the
    management path may transit its uplink); connection failures are
    "not yet", never a verdict.

    :return: dict with ``ok``, ``after`` (last :func:`get_vc_status`),
        ``fpc_state`` (str | None), ``interfaces`` (dict | None),
        ``booted`` (str | None, the member's boot timestamp when read),
        ``rebooted`` (bool, whether the reboot itself was observed),
        ``elapsed``, ``attempts``, ``error`` (``member_not_back`` /
        ``unreachable`` / None), ``error_message``. Does not print.
    """
    wanted = [n.strip() for n in expect_up if n and n.strip()]
    seen_down = False

    def check(dev):
        nonlocal seen_down
        status = get_vc_status(dev)
        snapshot = {"status": status, "fpc_state": None, "interfaces": None, "booted": None}
        if not status["ok"]:
            # An RPC/parse failure says nothing about the member, so it
            # must not count as "the member went down" for the
            # no-baseline fallback.
            return False, snapshot, f"{status['error']}: {status['error_message']}"
        entry = find_member(status, member)
        if entry is None or entry["status"] != "Prsnt" or not entry["role"]:
            seen_down = True
            seen = entry["status"] if entry else "absent"
            return False, snapshot, f"member {member} is {seen}"
        fpc_state = get_fpc_state(dev, member)
        snapshot["fpc_state"] = fpc_state
        if fpc_state != "Online":
            seen_down = True
            return False, snapshot, f"FPC {member} is {fpc_state or 'unknown'}, not Online"

        # Did the reboot actually happen? The RPC returns before the
        # member goes down, so "healthy right now" is not evidence.
        # Use this probe's master, not the pre-reboot one: mastership can
        # move during the window and it decides which uptime block
        # (fpcN vs localre) belongs to the member.
        booted = get_member_boot_time(dev, member, master=status.get("master") or master)
        snapshot["booted"] = booted
        if booted_before is not None:
            if booted is None:
                return False, snapshot, "cannot read the member's boot time"
            if not boot_time_is_newer(booted, booted_before):
                return False, snapshot, f"member {member} has not rebooted yet (booted {booted})"
        elif not seen_down:
            return False, snapshot, f"member {member} has not gone down yet"

        if wanted:
            states = get_interface_states(dev, wanted)
            snapshot["interfaces"] = states
            down = [f"{n}={v or 'unknown'}" for n, v in states.items() if v != "up/up"]
            if down:
                return False, snapshot, "interfaces not up: " + ", ".join(sorted(down))
        return True, snapshot, None

    def on_unreachable():
        nonlocal seen_down
        seen_down = True

    polled = _poll_device(hostname, timeout, interval, check, on_unreachable)
    last = polled["last"] or {}
    result = {
        "ok": polled["ok"],
        "after": last.get("status"),
        "fpc_state": last.get("fpc_state"),
        "interfaces": last.get("interfaces"),
        "booted": last.get("booted"),
        "rebooted": bool(polled["ok"]),
        "elapsed": polled["elapsed"],
        "attempts": polled["attempts"],
        "error": None,
        "error_message": polled["error_message"],
    }
    if not polled["ok"]:
        result["error"] = "member_not_back" if result["after"] is not None else "unreachable"
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
    start = time.monotonic()
    deadline = start + timeout
    last_problem = None
    while True:
        result["attempts"] += 1
        # Bound each probe by what is left of the window (min 1 s) so an
        # unreachable box cannot push the loop far past --wait.
        remaining = max(1, int(deadline - time.monotonic()))
        conn = common.connect(
            hostname, gather_facts=False, auto_probe=min(interval, remaining)
        )
        if conn["ok"]:
            dev = conn["dev"]
            try:
                status = get_vc_status(dev)
                if status["ok"]:
                    result["after"] = status
                    if status["master"] == str(expected):
                        result["ok"] = True
                        result["replication"] = get_replication_state(dev)
                        result["elapsed"] = int(time.monotonic() - start)
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
    result["elapsed"] = int(time.monotonic() - start)
    result["error"] = "mastership_unchanged" if result["after"] is not None else "unreachable"
    result["error_message"] = last_problem
    return result
