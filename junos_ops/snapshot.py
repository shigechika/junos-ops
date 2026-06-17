"""Create on-demand JUNOS recovery snapshots (``request system snapshot``).

A JUNOS upgrade only rewrites the currently-running boot media; the alternate
(backup) media is **not** refreshed automatically. Over successive upgrades the
alternate drifts and can end up many releases behind ("fossil alternate"). If
the primary media later fails to boot, the device falls back to that stale
alternate and may come up non-routable — turning a recoverable boot glitch into
an outage.

``request system snapshot`` syncs the alternate media to the running system.
This module exposes it as a standalone, on-demand operation so the alternate can
be refreshed deliberately (before a risky change, or on a schedule).

**Primary target: MX.** Both fixed-config MX (MX5/MX10/MX40/MX80, dual eUSB) and
RE/disk-based MX (MX240/MX480/MX960) are where the fossil-alternate problem
actually bites and where a manual snapshot is the real fix.

Per-personality behaviour (``dev.facts["personality"]``). All supported
personalities use the ``request-snapshot`` RPC (the same family the existing
:func:`junos_ops.upgrade.delete_snapshots` uses); verified on hardware that
``request system snapshot`` maps to ``<request-snapshot/>`` on MX5/SRX300:

- ``MX``         -> ``dev.rpc.request_snapshot()``                  (primary use case).
- ``SWITCH``     -> ``dev.rpc.request_snapshot()``                  (EX/QFX recovery
  snapshot). EX2300/EX3400 are chronically tight on free disk and the snapshot
  may not fit; an out-of-space failure is reported as a clean, non-fatal,
  operator-actionable result (not a crash). Note junos-ops already *deletes*
  snapshots on SWITCH to free space before an install, so creating one here is
  a deliberate, separate action.
- ``SRX_BRANCH`` -> ``dev.rpc.request_snapshot({"slice": "alternate"})``
  (low priority: branch SRX keep the alternate slice in sync during normal
  upgrades, so an explicit snapshot is usually unnecessary; supported for
  completeness). The positional-dict arg form avoids the kwarg->bool coercion
  trap documented in ``delete_snapshots``.
- everything else (vmhost MX such as MX204/MX10003, ``SRX_MIDRANGE`` /
  ``SRX_HIGHEND`` — SRX4600 confirmed to have no ``request system snapshot``
  command at all — HA clusters, vMX/vSRX, Junos Evolved, unknown) -> skipped as
  "unsupported / not verified on this platform", non-fatal, with **no RPC
  issued** (we do not guess on hardware we have not verified).

Safety guard: refuse to snapshot a device that is currently running on its
alternate/backup media (unless ``--force``), because snapshotting from the
alternate would clone a potentially stale system onto the primary — the exact
failure mode this feature exists to prevent.

.. warning::
   The on-device strings used by :func:`running_on_alternate_media` to detect
   the "booted from backup media" state **need field verification** across
   platforms. When detection is inconclusive the snapshot still proceeds but
   the result flags the uncertainty. This is the open design question tracked
   in issue #107.

Core functions return a result ``dict`` and never print.
"""

from logging import getLogger

from lxml import etree

from junos_ops import common

logger = getLogger(__name__)

# Supported personalities -> positional RPC args for ``request-snapshot``.
# ``None`` means call with no args (``request system snapshot``). A personality
# absent from this map is treated as unsupported (skipped, no RPC issued).
_SNAPSHOT_RPC_ARGS = {
    "MX": None,
    "SWITCH": None,
    "SRX_BRANCH": {"slice": "alternate"},
}

# Human-readable equivalent CLI command, for dry-run / messages.
_SNAPSHOT_LABEL = {
    "MX": "request system snapshot",
    "SWITCH": "request system snapshot",
    "SRX_BRANCH": "request system snapshot slice alternate",
}

# Snapshot copies the whole root partition to the alternate media and can take
# minutes on slow flash; use a generous default, overridable via --timeout.
DEFAULT_SNAPSHOT_TIMEOUT = 300


def running_on_alternate_media(dev) -> bool | None:
    """Best-effort: is the device currently booted from its alternate media?

    :return: ``True`` (running on the alternate/backup media), ``False``
        (running on the primary), or ``None`` (could not determine).

    .. warning::
       The matched strings need field verification across platforms (see the
       module-level warning / issue #107). On any error or unrecognized output
       this returns ``None`` so the caller can proceed with a warning rather
       than blocking on an unverified probe.
    """
    try:
        out = dev.cli("show system snapshot", warning=False)
    except Exception as e:  # noqa: BLE001 - any failure means inconclusive
        logger.debug("alternate-media probe failed: %s: %s", type(e).__name__, e)
        return None
    if not out:
        return None
    text = out.lower()
    # Indicators that the running system booted from the backup media. JUNOS
    # prints "running on alternate media device" as a login/boot NOTICE on
    # fixed-config platforms; other wordings are included defensively.
    # NEEDS FIELD VERIFICATION (issue #107).
    alt_markers = (
        "running on alternate media",
        "alternate media device",
        "booted from backup",
        "booted from the backup",
    )
    if any(m in text for m in alt_markers):
        return True
    # Output came back but showed no alternate marker -> assume primary.
    return False


def _is_out_of_space(message: str) -> bool:
    """Return True if an error message looks like an out-of-space failure."""
    low = message.lower()
    return (
        "not enough space" in low
        or "no space left" in low
        or ("insufficient" in low and "space" in low)
    )


def _classify_failure(result: dict, label: str, exc: Exception) -> dict:
    """Turn an RPC exception into a result dict.

    Out-of-space (common on space-tight EX2300/EX3400) is a clean, non-fatal,
    operator-actionable skip; anything else is a real failure (``ok=False``).
    """
    err_name = type(exc).__name__
    msg = str(exc)
    if _is_out_of_space(msg):
        result["ok"] = True
        result["skipped"] = True
        result["error"] = "no_space"
        result["message"] = (
            "snapshot skipped: not enough free space on the target media "
            "(common on space-tight EX2300/EX3400). " + msg
        ).strip()
        return result
    logger.warning("snapshot: %s: %s", err_name, exc)
    result["ok"] = False
    result["error"] = err_name
    result["message"] = f"snapshot failed: {err_name}: {exc}"
    return result


def create_snapshot(hostname, dev) -> dict:
    """Run ``request system snapshot`` on ``dev`` (sync the alternate media).

    :return: dict with keys ``hostname``, ``ok``, ``dry_run``, ``message``,
        ``error`` and ``steps`` (plus ``skipped`` for no-op / unsupported /
        no-space outcomes). Does not print.
    """
    result = {
        "hostname": hostname,
        "ok": False,
        "dry_run": common.args.dry_run,
        "message": None,
        "error": None,
        "steps": [],
    }

    personality = dev.facts.get("personality")
    if personality not in _SNAPSHOT_RPC_ARGS:
        # Unsupported / unverified platform: do not guess. Non-fatal skip.
        result["ok"] = True
        result["skipped"] = True
        result["message"] = (
            f"snapshot: unsupported / not verified on personality={personality!r} "
            f"(model={dev.facts.get('model')!r}) — skipped"
        )
        return result

    label = _SNAPSHOT_LABEL[personality]
    rpc_args = _SNAPSHOT_RPC_ARGS[personality]

    # Safety guard: refuse to snapshot a box booted from its alternate media —
    # that would clone a possibly-stale system onto the primary.
    on_alt = running_on_alternate_media(dev)
    force = getattr(common.args, "force", False)
    if on_alt is True and not force:
        result["ok"] = False
        result["error"] = "running_on_alternate_media"
        result["message"] = (
            "snapshot refused: device appears to be running on its alternate "
            "(backup) boot media. Snapshotting now would clone a possibly-stale "
            "system onto the primary. Re-run with --force only if the running "
            "image is the one you want to propagate."
        )
        return result
    if on_alt is None:
        result["steps"].append(
            {
                "action": "guard",
                "message": (
                    "snapshot: alternate-media check inconclusive — proceeding "
                    "(verify the device is on its primary media)"
                ),
            }
        )

    if common.args.dry_run:
        result["ok"] = True
        result["message"] = f"dry-run: {label}"
        return result

    timeout = getattr(common.args, "rpc_timeout", None) or DEFAULT_SNAPSHOT_TIMEOUT
    try:
        if rpc_args is None:
            rpc = dev.rpc.request_snapshot(dev_timeout=timeout)
        else:
            # Positional dict (not kwargs) to avoid the bool-coercion bug that
            # PyEZ hits with keyword args, as documented in delete_snapshots.
            rpc = dev.rpc.request_snapshot(rpc_args, dev_timeout=timeout)
    except Exception as e:  # noqa: BLE001 - report as a result, never crash
        return _classify_failure(result, label, e)

    xml_str = etree.tostring(rpc, encoding="unicode") if rpc is not None else ""
    logger.debug("snapshot: %s", xml_str)
    result["ok"] = True
    result["message"] = f"snapshot: '{label}' completed"
    return result
