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
stdout. This module consumes those dicts and renders human-friendly
terminal output.

Two parallel APIs:

- ``format_*(result) -> str`` — return the rendered text as a string
  without touching stdout. Non-CLI callers (e.g. ``junos-mcp``) use
  these to build MCP responses without needing ``contextlib.redirect_stdout``.

- ``print_*(result) -> None`` — thin wrappers that print the result of
  the corresponding ``format_*`` under :data:`_print_lock` so parallel
  execution (``--workers N``) produces readable, host-atomic output.
  These are what the CLI (``cmd_*``) uses.

Conventions:
    - ``format_*`` is the single source of truth for rendering; every
      ``print_*`` delegates to its ``format_*`` counterpart.
    - Some result dicts carry live PyEZ / lxml objects (``dev``, ``rpc``)
      that are not JSON-serializable. Display functions ignore those
      keys; programmatic consumers should strip them before serializing.
"""

import threading
from pprint import pformat

# Serializes stdout writes when cmd_* run in parallel via run_parallel.
_print_lock = threading.Lock()


# -------------------------------------------------------------------
# Infrastructure helpers
# -------------------------------------------------------------------


def _emit(text: str) -> None:
    """Acquire the print lock and write ``text`` to stdout.

    ``text`` may span multiple lines; the entire block is emitted
    atomically so parallel workers do not interleave each other's
    output. An empty string is skipped so callers do not need to
    guard against it.
    """
    if not text:
        return
    with _print_lock:
        print(text)


# -------------------------------------------------------------------
# Host header / footer / facts
# -------------------------------------------------------------------


def format_host_header(hostname: str) -> str:
    """Return the ``# hostname`` header line (no trailing newline)."""
    return f"# {hostname}"


def print_host_header(hostname: str) -> None:
    """Print the ``# hostname`` header used by every subcommand."""
    _emit(format_host_header(hostname))


def print_host_block(hostname: str, body: str) -> None:
    """Print ``# hostname`` header + ``body`` as one atomic block.

    Prevents parallel workers (``--workers N``) from interleaving another
    host's header between this host's header and its body.
    """
    header = format_host_header(hostname)
    text = header if not body else f"{header}\n{body}"
    _emit(text)


def format_host_footer() -> str:
    """Return an empty line (the separator used between hosts)."""
    return ""


def print_host_footer() -> None:
    """Print the blank line separator used between hosts."""
    with _print_lock:
        print("")


def format_facts(hostname: str, facts: dict) -> str:
    """Return the ``# host`` + pprint(facts) block as a string."""
    return f"# {hostname}\n{pformat(facts)}\n"


def print_facts(hostname: str, facts: dict) -> None:
    """Print device facts (used by ``cmd_facts``)."""
    _emit(format_facts(hostname, facts))


def pprint_facts(facts: dict) -> None:
    """Pretty-print ``dev.facts`` — kept for backwards compat with tests."""
    print(pformat(facts))


# -------------------------------------------------------------------
# Connection / config errors
# -------------------------------------------------------------------


def format_connect_error(result: dict) -> str:
    """Return the human-readable message for a failed connect dict."""
    msg = result.get("error_message")
    if msg is None:
        msg = f"connect failed: {result.get('error', 'unknown error')}"
    return msg


def print_connect_error(result: dict) -> None:
    """Print a NETCONF connection error from ``common.connect`` result."""
    _emit(format_connect_error(result))


def format_read_config_error(result: dict) -> str:
    """Return the human-readable message for a failed read_config dict."""
    msg = result.get("error") or "config read failed"
    path = result.get("path", "")
    return f"{msg}\n{path} is not ready"


def print_read_config_error(result: dict) -> None:
    """Print a config file read error from ``common.read_config`` result."""
    _emit(format_read_config_error(result))


# -------------------------------------------------------------------
# show_version
# -------------------------------------------------------------------


def format_version(result: dict) -> str:
    """Return ``upgrade.show_version`` result in the legacy text format."""
    lines: list[str] = []
    # Local/remote package check messages (previously printed directly
    # from check_local_package / check_remote_package; as of
    # junos-ops 0.14.1 they are carried in the dict's local_package /
    # remote_package fields and rendered here instead).
    local_pkg = result.get("local_package")
    if isinstance(local_pkg, dict) and local_pkg.get("message"):
        lines.append(local_pkg["message"])
    remote_pkg = result.get("remote_package")
    if isinstance(remote_pkg, dict) and remote_pkg.get("message"):
        lines.append(remote_pkg["message"])
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
    return "\n".join(lines)


def print_version(result: dict) -> None:
    """Print ``upgrade.show_version`` result in the legacy text format."""
    _emit(format_version(result))


# -------------------------------------------------------------------
# copy / rollback / reinstall / load_config
# -------------------------------------------------------------------


def _steps_text(result: dict, actions: set[str] | None = None) -> str:
    """Join the ``message`` field of each step into one multi-line string.

    :param actions: if given, only include steps whose ``action`` is in
        this set. If None, include all steps with a message.
    """
    lines: list[str] = []
    for step in result.get("steps", []):
        if actions is not None and step.get("action") not in actions:
            continue
        msg = step.get("message")
        if msg:
            lines.append(msg)
    return "\n".join(lines)


def format_copy(result: dict) -> str:
    """Return ``upgrade.copy`` result rendered as multi-line text.

    Walks the ``steps`` list in chronological order and emits each
    step's ``message``.
    """
    return _steps_text(result)


def print_copy(result: dict) -> None:
    """Print ``upgrade.copy`` result by walking its ``steps`` list."""
    _emit(format_copy(result))


def format_rollback(result: dict) -> str:
    """Return ``upgrade.rollback`` result as a single message block."""
    return result.get("message") or ""


def print_rollback(result: dict) -> None:
    """Print ``upgrade.rollback`` result."""
    _emit(format_rollback(result))


def format_reinstall(result: dict) -> str:
    """Return ``upgrade.check_and_reinstall`` result by walking steps."""
    return _steps_text(result)


def print_reinstall(result: dict) -> None:
    """Print ``upgrade.check_and_reinstall`` result by walking steps."""
    _emit(format_reinstall(result))


def format_load_config(result: dict) -> str:
    """Return ``upgrade.load_config`` result by walking its steps list."""
    return _steps_text(result)


def print_load_config(result: dict) -> None:
    """Print ``upgrade.load_config`` result by walking its steps list."""
    _emit(format_load_config(result))


# -------------------------------------------------------------------
# install (nested copy/rollback)
# -------------------------------------------------------------------


_INSTALL_PRE_ACTIONS = {"skip", "compare", "remote_check", "remote_missing"}
_INSTALL_POST_ACTIONS = {"clear_reboot", "rescue_save", "sw_install"}


def format_install(result: dict) -> str:
    """Return ``upgrade.install`` result including nested copy/rollback.

    Walks, in order:

    1. Steps before the rollback (``skip`` / ``compare`` / ``remote_check``).
    2. Nested ``rollback_result``, if any.
    3. Nested ``copy_result``, if any.
    4. Remaining steps (``clear_reboot`` / ``rescue_save`` / ``sw_install``).
    """
    parts: list[str] = []
    pre = _steps_text(result, _INSTALL_PRE_ACTIONS)
    if pre:
        parts.append(pre)
    rollback_result = result.get("rollback_result")
    if rollback_result:
        rb = format_rollback(rollback_result)
        if rb:
            parts.append(rb)
    copy_result = result.get("copy_result")
    if copy_result:
        cp = format_copy(copy_result)
        if cp:
            parts.append(cp)
    post = _steps_text(result, _INSTALL_POST_ACTIONS)
    if post:
        parts.append(post)
    return "\n".join(parts)


def print_install(result: dict) -> None:
    """Print ``upgrade.install`` result including nested copy/rollback."""
    _emit(format_install(result))


# -------------------------------------------------------------------
# reboot (nested reinstall)
# -------------------------------------------------------------------


_REBOOT_PRE_ACTIONS = {"existing_schedule", "force_clear", "clear_reboot"}


def format_reboot(result: dict) -> str:
    """Return ``upgrade.reboot`` result including nested reinstall output.

    Walks the pre-reinstall steps (existing schedule, force clear,
    clear_reboot), then the nested ``reinstall_result`` if any, then
    the final ``reboot`` step message.
    """
    parts: list[str] = []
    pre = _steps_text(result, _REBOOT_PRE_ACTIONS)
    if pre:
        parts.append(pre)
    reinstall = result.get("reinstall_result")
    if reinstall:
        ri = format_reinstall(reinstall)
        if ri:
            parts.append(ri)
    post = _steps_text(result, {"reboot"})
    if post:
        parts.append(post)
    return "\n".join(parts)


def print_reboot(result: dict) -> None:
    """Print ``upgrade.reboot`` result including nested reinstall output."""
    _emit(format_reboot(result))


# -------------------------------------------------------------------
# dry_run / list_remote / rsi
# -------------------------------------------------------------------


def format_dry_run(result: dict) -> str:
    """Return ``upgrade.dry_run`` result (debug-style summary)."""
    lines = [
        f"  - hostname: {result['hostname']}",
        f"  - model: {result['model']}",
        f"  - local file: {result['local_file']}",
        f"  - planning hash: {result['planning_hash']}",
        f"  - algo: {result['algo']}",
    ]
    local_pkg = result.get("local_package")
    if isinstance(local_pkg, dict) and local_pkg.get("message"):
        lines.append(local_pkg["message"])
    else:
        lines.append(f"  - local_package: {local_pkg}")
    remote_pkg = result.get("remote_package")
    if isinstance(remote_pkg, dict) and remote_pkg.get("message"):
        lines.append(remote_pkg["message"])
    else:
        lines.append(f"  - remote_package: {remote_pkg}")
    lines.append(f"  - ok: {result['ok']}")
    return "\n".join(lines)


def print_dry_run(result: dict) -> None:
    """Print ``upgrade.dry_run`` result (debug-style summary)."""
    _emit(format_dry_run(result))


def format_list_remote(result: dict) -> str:
    """Return ``upgrade.list_remote_path`` result in ``ls`` / ``ls -l`` format.

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
    return "\n".join(lines)


def print_list_remote(result: dict) -> None:
    """Print ``upgrade.list_remote_path`` result."""
    _emit(format_list_remote(result))


def format_rsi(result: dict) -> str:
    """Return ``rsi.collect_rsi`` result (one line per artifact written)."""
    lines: list[str] = []
    hostname = result.get("hostname", "")
    if result.get("scf"):
        lines.append(f"  {hostname}.SCF done")
    if result.get("rsi"):
        lines.append(f"  {hostname}.RSI done")
    if not result.get("ok") and result.get("error_message"):
        lines.append(f"  {hostname}: {result['error']}: {result['error_message']}")
    return "\n".join(lines)


def print_rsi(result: dict) -> None:
    """Print ``rsi.collect_rsi`` result (one line per file written)."""
    _emit(format_rsi(result))


# -------------------------------------------------------------------
# show (not yet migrated)
# -------------------------------------------------------------------


def print_show(result: dict) -> None:
    """Print ``cmd_show`` result (a list of command/output pairs)."""
    raise NotImplementedError


# -------------------------------------------------------------------
# check
# -------------------------------------------------------------------


def _short_check_status(sub: dict | None) -> str:
    """Render a local/remote check sub-result as a short column label."""
    if sub is None:
        return "-"
    status = sub.get("status", "-")
    if status == "ok" and sub.get("cached"):
        return "ok(cached)"
    return status


def _short_connect_status(sub: dict | None) -> str:
    """Render a connect sub-result as ``ok`` / ``fail`` / ``-``."""
    if sub is None:
        return "-"
    return "ok" if sub.get("ok") else "fail"


def format_check_table(
    rows: list[dict],
    *,
    show_connect: bool = True,
    show_local: bool = False,
    show_remote: bool = False,
) -> str:
    """Render ``check`` subcommand results as an aligned table.

    Each row is the dict returned by the ``check`` worker with keys
    ``hostname``, ``model``, ``model_source``, ``connect``, ``local``,
    ``remote``. Columns are included based on the ``show_*`` flags so
    a single-aspect run (e.g. ``check --connect``) does not emit empty
    columns.
    """
    headers: list[str] = ["hostname"]
    if show_connect:
        headers.append("connect")
    if show_local:
        headers.append("local")
    if show_remote:
        headers.append("remote")
    headers.extend(["model", "file"])

    body: list[list[str]] = []
    for row in rows:
        line = [row.get("hostname") or "-"]
        if show_connect:
            line.append(_short_connect_status(row.get("connect")))
        if show_local:
            line.append(_short_check_status(row.get("local")))
        if show_remote:
            line.append(_short_check_status(row.get("remote")))
        line.append(row.get("model") or "-")
        file_val = None
        for key in ("local", "remote"):
            sub = row.get(key)
            if sub and sub.get("file"):
                file_val = sub["file"]
                break
        line.append(file_val or "-")
        body.append(line)

    widths = [
        max(len(headers[i]), *(len(r[i]) for r in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]

    def _fmt_row(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    lines = [_fmt_row(headers), _fmt_row(["-" * w for w in widths])]
    lines.extend(_fmt_row(r) for r in body)

    # Surface detailed failure messages below the table. ``missing`` is
    # already obvious from the status + file columns above, so only
    # ``bad`` (checksum mismatch) and ``error`` (recipe lookup, RPC,
    # etc.) earn a detail line; connect failures always do.
    detail_lines: list[str] = []
    for row in rows:
        host = row.get("hostname") or "?"
        conn = row.get("connect")
        if conn and not conn.get("ok"):
            msg = conn.get("message") or conn.get("error") or "connect failed"
            detail_lines.append(f"  {host}: connect: {msg}")
        for key in ("local", "remote"):
            sub = row.get(key)
            if sub and sub.get("status") in ("bad", "error"):
                detail_lines.append(
                    f"  {host}: {key}: {sub.get('message', '').lstrip()}"
                )
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)

    return "\n".join(lines)


def print_check_table(
    rows: list[dict],
    *,
    show_connect: bool = True,
    show_local: bool = False,
    show_remote: bool = False,
) -> None:
    """Print ``check`` subcommand results as an aligned table."""
    _emit(format_check_table(
        rows,
        show_connect=show_connect,
        show_local=show_local,
        show_remote=show_remote,
    ))


def format_check_local_inventory(rows: list[dict]) -> str:
    """Render ``check --local`` inventory results (one row per model).

    ``rows`` come from :func:`cli._check_local_inventory`. Columns:
    ``model`` / ``file`` / ``status``. When any row resolves its
    firmware under a non-empty ``lpath`` directory, that prefix is
    shown once above the table (``lpath: /path``) instead of being
    duplicated into every row. Failure detail messages (bad / missing
    / error) are appended below.
    """
    headers = ["model", "file", "status"]
    body: list[list[str]] = []
    for r in rows:
        status = r.get("status", "-")
        if status == "ok" and r.get("cached"):
            status = "ok(cached)"
        body.append([
            r.get("model") or "-",
            r.get("file") or "-",
            status,
        ])

    widths = [
        max(len(headers[i]), *(len(b[i]) for b in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]

    def _fmt_row(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    lines: list[str] = []
    # Surface a single lpath header instead of repeating it per row.
    lpaths = {
        r.get("local_file", "").rsplit("/", 1)[0]
        for r in rows
        if r.get("local_file") and "/" in r.get("local_file", "")
        and r.get("local_file") != r.get("file")
    }
    if len(lpaths) == 1:
        lines.append(f"lpath: {lpaths.pop()}")
    elif len(lpaths) > 1:
        # Rare: host-section overrides mix lpaths — fall back to per-row.
        lines.extend(f"lpath[{r.get('model')}]: {r.get('local_file')}" for r in rows)

    lines.append(_fmt_row(headers))
    lines.append(_fmt_row(["-" * w for w in widths]))
    lines.extend(_fmt_row(r) for r in body)

    # ``missing`` rows are already self-explanatory from the file +
    # status columns; only ``bad`` / ``error`` carry information not
    # already in the table, so reserve detail lines for those.
    detail_lines: list[str] = []
    for r in rows:
        if r.get("status") in ("bad", "error"):
            msg = (r.get("message") or "").lstrip()
            detail_lines.append(f"  {r.get('model')}: {msg}")
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)

    return "\n".join(lines)


def print_check_local_inventory(rows: list[dict]) -> None:
    """Print ``check --local`` inventory table."""
    _emit(format_check_local_inventory(rows))
