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
    """Print a NETCONF connection error from ``common.connect`` result."""
    raise NotImplementedError


def print_read_config_error(result: dict) -> None:
    """Print a config file read error from ``common.read_config`` result."""
    raise NotImplementedError


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
    """Print ``upgrade.copy`` result (walks ``steps`` list)."""
    raise NotImplementedError


def print_install(result: dict) -> None:
    """Print ``upgrade.install`` result (including nested copy/rollback)."""
    raise NotImplementedError


def print_rollback(result: dict) -> None:
    """Print ``upgrade.rollback`` result."""
    raise NotImplementedError


def print_reboot(result: dict) -> None:
    """Print ``upgrade.reboot`` result (including nested reinstall result)."""
    raise NotImplementedError


def print_dry_run(result: dict) -> None:
    """Print ``upgrade.dry_run`` result."""
    raise NotImplementedError


def print_list_remote(result: dict) -> None:
    """Print ``upgrade.list_remote_path`` result."""
    raise NotImplementedError


def print_load_config(result: dict) -> None:
    """Print ``upgrade.load_config`` result (walks ``steps`` list)."""
    raise NotImplementedError


def print_rsi(result: dict) -> None:
    """Print ``rsi.collect_rsi`` result."""
    raise NotImplementedError


def print_show(result: dict) -> None:
    """Print ``cmd_show`` result (a list of command/output pairs)."""
    raise NotImplementedError
