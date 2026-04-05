"""Tests for junos_ops.display.

As of junos-ops 0.14.1 the display layer exposes two parallel APIs:

- ``format_*(result) -> str`` returns the rendered text without
  touching stdout. This is what non-CLI consumers (e.g. junos-mcp)
  call to build responses without needing ``contextlib.redirect_stdout``.
- ``print_*(result)`` prints ``format_*`` under ``_print_lock``.

Every ``print_*`` is a thin wrapper over ``format_*``, so the tests
exercise both the formatting logic (via ``format_*``) and the
side-effecting wrapper (via ``print_*`` + ``capsys``).
"""

from junos_ops import display


# -------------------------------------------------------------------
# host header / footer / facts
# -------------------------------------------------------------------


def test_print_host_header(capsys):
    display.print_host_header("rt1.example.jp")
    captured = capsys.readouterr()
    assert captured.out == "# rt1.example.jp\n"


def test_format_host_header():
    assert display.format_host_header("rt1.example.jp") == "# rt1.example.jp"


def test_print_host_footer(capsys):
    display.print_host_footer()
    captured = capsys.readouterr()
    assert captured.out == "\n"


def test_format_host_footer():
    assert display.format_host_footer() == ""


def test_print_facts(capsys):
    display.print_facts("rt1.example.jp", {"model": "MX240", "version": "21.4R3"})
    captured = capsys.readouterr()
    assert "# rt1.example.jp" in captured.out
    assert "MX240" in captured.out
    assert "21.4R3" in captured.out


def test_format_facts():
    text = display.format_facts("rt1.example.jp", {"model": "MX240"})
    assert "# rt1.example.jp" in text
    assert "MX240" in text


# -------------------------------------------------------------------
# connect / read_config errors
# -------------------------------------------------------------------


def test_format_connect_error_with_message():
    result = {
        "hostname": "rt1",
        "host": "rt1",
        "ok": False,
        "dev": None,
        "error": "ConnectRefusedError",
        "error_message": "NETCONF Connection refused: 10.0.0.1",
    }
    text = display.format_connect_error(result)
    assert "NETCONF Connection refused" in text
    assert "10.0.0.1" in text


def test_format_connect_error_fallback_to_class_name():
    result = {"ok": False, "error": "ConnectError", "error_message": None}
    text = display.format_connect_error(result)
    assert "ConnectError" in text


def test_format_read_config_error():
    result = {"ok": False, "path": "/tmp/bad.ini", "error": "/tmp/bad.ini is empty"}
    text = display.format_read_config_error(result)
    assert "is empty" in text
    assert "/tmp/bad.ini is not ready" in text


# -------------------------------------------------------------------
# show_version: local/remote package messages now rendered from dict
# -------------------------------------------------------------------


def _fake_version_result(local_pkg_status=None, remote_pkg_status=None):
    def _pkg(status):
        if status is None:
            return None
        messages = {
            "ok": "  - local package: X.tgz is found. checksum is OK.",
            "missing": "  - local package: X.tgz is not found.",
        }
        return {
            "hostname": "rt1",
            "file": "X.tgz",
            "local_file": "X.tgz",
            "algo": "md5",
            "expected_hash": "abc",
            "actual_hash": "abc" if status == "ok" else None,
            "status": status,
            "cached": False,
            "message": messages.get(status, f"  - local package: status={status}"),
            "error": None,
        }
    def _remote_pkg(status):
        if status is None:
            return None
        return {
            "hostname": "rt1",
            "file": "X.tgz",
            "remote_path": "/var/tmp",
            "algo": "md5",
            "expected_hash": "abc",
            "actual_hash": "abc" if status == "ok" else None,
            "status": status,
            "cached": False,
            "message": (
                "  - remote package: X.tgz is found. checksum is OK."
                if status == "ok" else f"  - remote package: status={status}"
            ),
            "error": None,
        }
    return {
        "hostname": "rt1",
        "model": "EX2300-24T",
        "running": "22.4R3-S6.5",
        "planning": "22.4R3-S6.5",
        "pending": None,
        "running_vs_planning": 0,
        "running_vs_pending": None,
        "commit": None,
        "rescue_config_epoch": None,
        "config_changed_after_install": False,
        "local_package": _pkg(local_pkg_status),
        "remote_package": _remote_pkg(remote_pkg_status),
        "reboot_scheduled": None,
    }


def test_format_version_includes_package_check_lines():
    """show_version dict now carries local/remote package dicts; the
    display layer must surface their messages (previously printed
    directly from the helpers)."""
    result = _fake_version_result(local_pkg_status="ok", remote_pkg_status="ok")
    text = display.format_version(result)
    assert "local package" in text
    assert "remote package" in text
    assert "checksum is OK" in text
    assert "running version: 22.4R3-S6.5" in text
    assert "model: EX2300-24T" in text


def test_format_version_omits_missing_package_dicts():
    """When the helpers return None (e.g. config missing srx4600.file)
    the version block must still render without raising."""
    result = _fake_version_result(local_pkg_status=None, remote_pkg_status=None)
    text = display.format_version(result)
    assert "local package" not in text
    assert "remote package" not in text
    assert "running version" in text  # main body still renders


# -------------------------------------------------------------------
# dry_run
# -------------------------------------------------------------------


def test_format_dry_run_with_nested_package_dicts():
    result = {
        "hostname": "rt1",
        "model": "EX2300-24T",
        "local_file": "/tmp/X.tgz",
        "planning_hash": "abc",
        "algo": "md5",
        "local_package": {"status": "ok", "message": "  - local package: X.tgz is found. checksum is OK."},
        "remote_package": {"status": "missing", "message": "  - remote package: X.tgz is not found."},
        "ok": False,
    }
    text = display.format_dry_run(result)
    assert "checksum is OK" in text
    assert "is not found" in text
    assert "ok: False" in text


# -------------------------------------------------------------------
# copy / install / rollback / reboot: format vs print parity
# -------------------------------------------------------------------


def test_format_copy_walks_steps():
    result = {
        "hostname": "rt1",
        "ok": True,
        "steps": [
            {"action": "storage_cleanup", "message": "cleanup done"},
            {"action": "scp", "message": "scp done"},
        ],
        "error": None,
    }
    text = display.format_copy(result)
    assert text == "cleanup done\nscp done"


def test_format_rollback():
    assert display.format_rollback({"message": "rollback: ok"}) == "rollback: ok"
    assert display.format_rollback({"message": ""}) == ""
    assert display.format_rollback({}) == ""


def test_format_install_nests_copy_and_rollback():
    copy_result = {"steps": [{"action": "scp", "message": "scp done"}]}
    rollback_result = {"message": "rollback successful"}
    result = {
        "hostname": "rt1",
        "ok": True,
        "rollback_result": rollback_result,
        "copy_result": copy_result,
        "steps": [
            {"action": "compare", "message": "compare: equal"},
            {"action": "rescue_save", "message": "rescue saved"},
            {"action": "sw_install", "message": "sw install ok"},
        ],
    }
    text = display.format_install(result)
    # Order: pre-steps (compare) → rollback → copy → post-steps (rescue/sw_install)
    lines = text.split("\n")
    assert "compare: equal" in text
    assert lines.index("compare: equal") < lines.index("rollback successful")
    assert lines.index("rollback successful") < lines.index("scp done")
    assert lines.index("scp done") < lines.index("rescue saved")


def test_format_reboot_nests_reinstall():
    reinstall_result = {
        "steps": [{"action": "warning", "message": "\tWARNING: config changed"}],
    }
    result = {
        "hostname": "rt1",
        "ok": True,
        "code": 0,
        "reinstall_result": reinstall_result,
        "steps": [
            {"action": "existing_schedule", "message": "\texisting reboot found"},
            {"action": "reboot", "message": "\treboot at ..."},
        ],
    }
    text = display.format_reboot(result)
    lines = text.split("\n")
    # existing_schedule → reinstall warning → reboot
    assert "existing reboot found" in text
    assert "WARNING: config changed" in text
    assert "reboot at" in text
    assert lines.index("\texisting reboot found") < lines.index("\tWARNING: config changed")
    assert lines.index("\tWARNING: config changed") < lines.index("\treboot at ...")


# -------------------------------------------------------------------
# list_remote / rsi / load_config
# -------------------------------------------------------------------


def test_format_list_remote_long():
    result = {
        "hostname": "rt1",
        "path": "/var/tmp",
        "files": [
            {
                "name": "foo.tgz",
                "type": "file",
                "path": "/var/tmp/foo.tgz",
                "size": 123,
                "owner": "root",
                "permissions_text": "-rw-r--r--",
                "ts_date": "Jan  1 00:00",
            },
        ],
        "file_count": 1,
        "format": "long",
    }
    text = display.format_list_remote(result)
    assert "/var/tmp:" in text
    assert "-rw-r--r--" in text
    assert "total files: 1" in text


def test_format_list_remote_short():
    result = {
        "hostname": "rt1",
        "path": "/var/tmp",
        "files": [
            {"name": "foo", "type": "file", "path": "/var/tmp/foo"},
            {"name": "sub", "type": "dir", "path": "/var/tmp/sub"},
        ],
        "file_count": 2,
        "format": "short",
    }
    text = display.format_list_remote(result)
    assert "/var/tmp/foo" in text
    assert "/var/tmp/sub/" in text
    assert "total files" not in text


def test_format_rsi_both_success():
    result = {
        "hostname": "rt1",
        "ok": True,
        "scf": {"path": "/tmp/rt1.SCF", "bytes": 100, "command": "show configuration"},
        "rsi": {"path": "/tmp/rt1.RSI", "bytes": 2000},
        "error": None,
    }
    text = display.format_rsi(result)
    assert "rt1.SCF done" in text
    assert "rt1.RSI done" in text


def test_format_rsi_rpc_failure():
    result = {
        "hostname": "rt1",
        "ok": False,
        "scf": {"path": "/tmp/rt1.SCF", "bytes": 50, "command": "show configuration"},
        "rsi": None,
        "error": "rsi_rpc",
        "error_message": "RPC timeout",
    }
    text = display.format_rsi(result)
    assert "rt1.SCF done" in text
    assert "rt1.RSI done" not in text
    assert "rsi_rpc" in text
    assert "RPC timeout" in text


def test_format_load_config_walks_steps():
    result = {
        "ok": True,
        "steps": [
            {"action": "commit_check", "message": "\tcommit check passed"},
            {"action": "commit_confirmed", "message": "\tcommit confirmed 1 applied"},
        ],
    }
    text = display.format_load_config(result)
    assert "commit check passed" in text
    assert "commit confirmed 1 applied" in text


# -------------------------------------------------------------------
# print_* = format_* parity (no-stdout-leak + format match)
# -------------------------------------------------------------------


def test_print_version_matches_format_version(capsys):
    """print_X(result) must print exactly format_X(result) + trailing \\n."""
    result = _fake_version_result(local_pkg_status="ok", remote_pkg_status="ok")
    display.print_version(result)
    captured = capsys.readouterr()
    assert captured.out == display.format_version(result) + "\n"


def test_print_copy_matches_format_copy(capsys):
    result = {
        "ok": True,
        "steps": [
            {"action": "scp", "message": "copy done"},
        ],
    }
    display.print_copy(result)
    captured = capsys.readouterr()
    assert captured.out == display.format_copy(result) + "\n"


def test_print_rollback_empty_message_emits_nothing(capsys):
    display.print_rollback({"message": ""})
    assert capsys.readouterr().out == ""

