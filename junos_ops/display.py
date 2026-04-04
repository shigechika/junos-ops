#
#   Copyright ©︎2022-2026 AIKAWA Shigechika
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Human-readable display layer for junos-ops result dicts.

Core functions in :mod:`junos_ops.upgrade`, :mod:`junos_ops.common`, and
:mod:`junos_ops.rsi` return plain ``dict`` values without printing to
stdout. This module consumes those dicts and prints human-friendly
terminal output. It is intentionally separate so that non-CLI callers
(e.g. junos-mcp) can opt out of all stdout writes simply by not
importing it.

Conventions:
    - Every function takes a dict and prints to stdout.
    - Functions acquire :data:`_print_lock` around multi-line writes so
      parallel execution (``--workers N``) produces readable output.
    - Some result dicts carry live PyEZ / lxml objects (``dev``, ``rpc``)
      that are not JSON-serializable. Display functions ignore those
      keys; programmatic consumers should strip them before serializing.
"""

import threading
from pprint import pformat

# Serializes stdout writes when cmd_* run in parallel via run_parallel.
_print_lock = threading.Lock()


def print_host_header(hostname: str) -> None:
    """Print the ``# hostname`` header used by every subcommand."""
    with _print_lock:
        print(f"# {hostname}")


def print_host_footer() -> None:
    """Print the blank line separator used between hosts."""
    with _print_lock:
        print("")


def print_connect_error(result: dict) -> None:
    """Print a NETCONF connection error from ``common.connect`` result.

    Expects ``result["ok"] is False``. Prints ``error_message`` verbatim,
    preserving the legacy single-line format from the pre-dict implementation.
    """
    msg = result.get("error_message")
    if msg is None:
        msg = f"connect failed: {result.get('error', 'unknown error')}"
    with _print_lock:
        print(msg)


def print_read_config_error(result: dict) -> None:
    """Print a config file read error from ``common.read_config`` result.

    Expects ``result["ok"] is False``.
    """
    msg = result.get("error") or "config read failed"
    with _print_lock:
        print(msg)
        print(f"{result.get('path', '')} is not ready")


def print_facts(hostname: str, facts: dict) -> None:
    """Print device facts (used by ``cmd_facts``)."""
    with _print_lock:
        print(f"# {hostname}")
        pprint_facts(facts)
        print("")


def pprint_facts(facts: dict) -> None:
    """Pretty-print ``dev.facts`` — split out so tests can call it directly."""
    print(pformat(facts))


def print_version(result: dict) -> None:
    """Print ``upgrade.show_version`` result in the legacy text format."""
    lines = []
    lines.append(f"  - hostname: {result['hostname']}")
    lines.append(f"  - model: {result['model']}")
    running = result["running"]
    planning = result["planning"]
    pending = result["pending"]
    lines.append(f"  - running version: {running}")
    lines.append(f"  - planning version: {planning}")
    cmp_plan = result["running_vs_planning"]
    if cmp_plan == 1:
        lines.append(f"    - {running=} > {planning=}")
    elif cmp_plan == -1:
        lines.append(f"    - {running=} < {planning=}")
    elif cmp_plan == 0:
        lines.append(f"    - {running=} = {planning=}")
    lines.append(f"  - pending version: {pending}")
    cmp_pend = result["running_vs_pending"]
    if cmp_pend == 1:
        lines.append(f"    - {running=} > {pending=} : Do you want to rollback?")
    elif cmp_pend == -1:
        lines.append(f"    - {running=} < {pending=} : Please plan to reboot.")
    elif cmp_pend == 0:
        lines.append(f"    - {running=} = {pending=}")
    commit = result.get("commit")
    if commit is not None:
        lines.append(
            f"  - last commit: {commit['datetime']} by {commit['user']} "
            f"via {commit['client']}"
        )
        if result.get("config_changed_after_install"):
            lines.append(
                "    - WARNING: config modified after firmware install. "
                "Re-install will run on reboot."
            )
    rebooting = result.get("reboot_scheduled")
    if rebooting is not None:
        lines.append(f"  - {rebooting}")
    with _print_lock:
        for line in lines:
            print(line)


def print_copy(result: dict) -> None:
    """Print ``upgrade.copy`` result by walking its ``steps`` list.

    Each step carries a ``message`` field that mirrors the legacy
    single-line output. Messages with no content are silently skipped.
    """
    lines = []
    for step in result.get("steps", []):
        msg = step.get("message")
        if msg:
            lines.append(msg)
    if not lines:
        return
    with _print_lock:
        for line in lines:
            print(line)


def print_install(result: dict) -> None:
    """Print ``upgrade.install`` result including nested copy/rollback.

    Walks, in order:

    1. Steps before the rollback (e.g. ``skip`` / ``compare``).
    2. Nested rollback result, if any.
    3. Nested copy result, if any (via :func:`print_copy`).
    4. Remaining steps (rescue_save, sw_install, ...).
    """
    steps = result.get("steps", [])
    lines: list[str] = []

    # Pre-rollback steps
    for step in steps:
        if step.get("action") in ("skip", "compare", "remote_check"):
            msg = step.get("message")
            if msg:
                lines.append(msg)
    if lines:
        with _print_lock:
            for line in lines:
                print(line)

    # Nested rollback
    rollback_result = result.get("rollback_result")
    if rollback_result:
        print_rollback(rollback_result)

    # Nested copy
    copy_result = result.get("copy_result")
    if copy_result:
        print_copy(copy_result)

    # Post-copy steps (rescue_save, sw_install, ...)
    post_lines: list[str] = []
    for step in steps:
        if step.get("action") in ("rescue_save", "sw_install"):
            msg = step.get("message")
            if msg:
                post_lines.append(msg)
    if post_lines:
        with _print_lock:
            for line in post_lines:
                print(line)


def print_rollback(result: dict) -> None:
    """Print ``upgrade.rollback`` result.

    Mirrors the legacy single-message behaviour: prints ``message`` as-is
    (it already contains the XML body on success and the exception text
    on failure).
    """
    msg = result.get("message")
    if not msg:
        return
    with _print_lock:
        print(msg)


def print_reboot(result: dict) -> None:
    """Print ``upgrade.reboot`` result including nested reinstall output.

    Walks steps up to the ``reinstall_result`` boundary, prints the
    nested :func:`print_reinstall` output if present, then emits the
    remaining steps (the actual reboot schedule message).
    """
    steps = result.get("steps", [])
    # Pre-reinstall: existing schedule / force clear / warnings
    pre_actions = {"existing_schedule", "force_clear"}
    pre = [s["message"] for s in steps if s.get("action") in pre_actions and s.get("message")]
    if pre:
        with _print_lock:
            for line in pre:
                print(line)

    reinstall = result.get("reinstall_result")
    if reinstall:
        print_reinstall(reinstall)

    post = [
        s["message"] for s in steps
        if s.get("action") == "reboot" and s.get("message")
    ]
    if post:
        with _print_lock:
            for line in post:
                print(line)


def print_reinstall(result: dict) -> None:
    """Print ``upgrade.check_and_reinstall`` result by walking steps."""
    lines = [s["message"] for s in result.get("steps", []) if s.get("message")]
    if not lines:
        return
    with _print_lock:
        for line in lines:
            print(line)


def print_dry_run(result: dict) -> None:
    """Print ``upgrade.dry_run`` result (debug-style summary)."""
    lines = [
        f"  - hostname: {result['hostname']}",
        f"  - model: {result['model']}",
        f"  - local file: {result['local_file']}",
        f"  - planning hash: {result['planning_hash']}",
        f"  - algo: {result['algo']}",
        f"  - local_package: {result['local_package']}",
        f"  - remote_package: {result['remote_package']}",
        f"  - ok: {result['ok']}",
    ]
    with _print_lock:
        for line in lines:
            print(line)


def print_list_remote(result: dict) -> None:
    """Print ``upgrade.list_remote_path`` result.

    Honours the ``format`` field ("short" or "long"). ``long`` mirrors
    ``ls -l`` style and appends a ``total files`` footer; ``short`` lists
    one path per line and appends a ``/`` to directories.
    """
    lines = [f"{result['path']}:"]
    fmt = result.get("format") or "short"
    if fmt == "short":
        for entry in result["files"]:
            if entry.get("type") == "file":
                lines.append(entry.get("path", entry.get("name", "")))
            else:
                lines.append((entry.get("path") or entry.get("name", "")) + "/")
    else:
        for entry in result["files"]:
            lines.append(
                "%s %s %9d %s %s"
                % (
                    entry.get("permissions_text"),
                    entry.get("owner"),
                    entry.get("size") or 0,
                    entry.get("ts_date"),
                    entry.get("path"),
                )
            )
        lines.append("total files: %d" % (result.get("file_count") or 0))
    with _print_lock:
        for line in lines:
            print(line)


def print_load_config(result: dict) -> None:
    """Print ``upgrade.load_config`` result (walks ``steps`` list)."""
    raise NotImplementedError


def print_rsi(result: dict) -> None:
    """Print ``rsi.collect_rsi`` result."""
    raise NotImplementedError


def print_show(result: dict) -> None:
    """Print ``cmd_show`` result (a list of command/output pairs)."""
    raise NotImplementedError
