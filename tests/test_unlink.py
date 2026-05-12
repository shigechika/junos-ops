"""Tests for the --unlink path via dev.cli().

Covers ``upgrade._install_via_cli_with_unlink`` and the dispatch in
``upgrade.install`` driven by ``common.args.unlink``.
"""

from unittest.mock import MagicMock

import pytest

from junos_ops import upgrade


# -------------------------------------------------------------------
# _install_via_cli_with_unlink
# -------------------------------------------------------------------


def _make_dev(cli_output, timeout=30):
    """Build a minimal dev mock returning ``cli_output`` from dev.cli()."""
    dev = MagicMock()
    dev.timeout = timeout
    dev.cli.return_value = cli_output
    return dev


def test_unlink_success_set_will_be_activated():
    output = (
        "Verified junos-arm-32-23.4R2-S7.4 ...\n"
        "Adding junos-arm-32-23.4R2-S7.4 ...\n"
        "Mounting junos-runtime-arm-32-...\n"
        "Validation succeeded\n"
        "NOTICE: 'pending' set will be activated at next reboot...\n"
    )
    dev = _make_dev(output)
    status, msg = upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/junos-arm-32-23.4R2-S7.4.tgz"
    )
    assert status is True
    assert "set will be activated" in msg
    # The exact CLI string was issued
    cli_cmd = dev.cli.call_args[0][0]
    assert "request system software add" in cli_cmd
    assert "unlink" in cli_cmd
    assert "/var/tmp/junos-arm-32-23.4R2-S7.4.tgz" in cli_cmd
    # timeout is restored
    assert dev.timeout == 30


def test_unlink_failure_insufficient_space():
    output = (
        "Adding junos-runtime-ex-arm-32-...\n"
        "ERROR: insufficient space for /packages/db/junos-arm-32-23.4R2-S7.4/contents/junos-runtime.tgz\n"
        "ERROR: insufficient space\n"
    )
    dev = _make_dev(output)
    status, msg = upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/junos-arm-32-23.4R2-S7.4.tgz"
    )
    assert status is False
    assert "ERROR" in msg
    assert "insufficient space" in msg


def test_unlink_empty_output():
    dev = _make_dev("")
    status, msg = upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/x.tgz"
    )
    assert status is False
    assert msg == "no output from CLI"


def test_unlink_cli_exception():
    dev = MagicMock()
    dev.timeout = 30
    dev.cli.side_effect = RuntimeError("connection lost")
    status, msg = upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/x.tgz"
    )
    assert status is False
    assert "RuntimeError" in msg
    assert "connection lost" in msg
    # timeout still restored even on exception
    assert dev.timeout == 30


def test_unlink_success_but_with_error_line_is_treated_as_failure():
    # If both success markers and ERROR appear, treat as failure (safety).
    output = (
        "Validation succeeded\n"
        "ERROR: something unexpected\n"
        "NOTICE: 'pending' set will be activated at next reboot...\n"
    )
    dev = _make_dev(output)
    status, msg = upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/x.tgz"
    )
    assert status is False
    assert "ERROR" in msg


def test_unlink_passes_dev_timeout():
    output = "Validation succeeded\nNOTICE: 'pending' set will be activated at next reboot...\n"
    dev = _make_dev(output, timeout=60)
    upgrade._install_via_cli_with_unlink(
        "rt1.example.jp", dev, "/var/tmp/x.tgz", timeout=1200
    )
    # dev.timeout was set to 1200 during the call, then restored to 60
    assert dev.timeout == 60


# -------------------------------------------------------------------
# install() dispatch with unlink flag
# -------------------------------------------------------------------


@pytest.fixture
def install_setup(monkeypatch, mock_args, mock_config, junos_common):
    """Set up a minimal environment for upgrade.install() to reach the
    'request system software add' step.

    Stubs out everything before sw_install so the test can focus on the
    --unlink dispatch.
    """
    # bypass pre-flight checks
    monkeypatch.setattr(
        upgrade, "check_running_package", lambda h, d: {"match": False}
    )
    monkeypatch.setattr(upgrade, "get_pending_version", lambda h, d: None)
    monkeypatch.setattr(
        upgrade,
        "check_remote_package",
        lambda h, d: {"status": "ok", "message": "ok"},
    )
    monkeypatch.setattr(
        upgrade,
        "copy",
        lambda h, d: {"ok": True, "steps": []},
    )
    monkeypatch.setattr(
        upgrade,
        "clear_reboot",
        lambda d: {"ok": True, "message": "clear_reboot ok"},
    )

    # rescue_save uses Config(dev).rescue("save") — mock the Config import
    from jnpr.junos.utils import config as pyez_config

    class _FakeConfig:
        def __init__(self, dev):
            pass

        def rescue(self, action):
            return True

    monkeypatch.setattr(pyez_config, "Config", _FakeConfig)
    monkeypatch.setattr(upgrade, "Config", _FakeConfig)

    # get_model_file / get_model_hash with simple lookups
    monkeypatch.setattr(
        upgrade,
        "get_model_file",
        lambda h, m: "junos-arm-32-23.4R2-S7.4.tgz",
    )
    monkeypatch.setattr(
        upgrade,
        "get_model_hash",
        lambda h, m: "f36cf2d79c3b91eb93d04083169ee06e",
    )

    mock_args.subcommand = "install"
    # Build a fake dev
    dev = MagicMock()
    dev.facts = {"model": "EX3400-24T"}
    dev.timeout = 30
    return mock_args, dev


def test_install_with_unlink_calls_cli(install_setup, monkeypatch):
    args, dev = install_setup
    args.unlink = True

    captured = {}

    def fake_cli_unlink(hostname, dev_arg, file_path, timeout=2400):
        captured["called"] = True
        captured["file_path"] = file_path
        return True, "set will be activated at next reboot"

    monkeypatch.setattr(
        upgrade, "_install_via_cli_with_unlink", fake_cli_unlink
    )
    # SW should NOT be used; if it is, the test fails because SW(dev).install()
    # raises (no real device).
    result = upgrade.install("test-host", dev)

    assert result["ok"] is True
    assert result["unlink_used"] is True
    assert captured.get("called") is True
    assert captured["file_path"].endswith("/junos-arm-32-23.4R2-S7.4.tgz")


def test_install_without_unlink_uses_sw(install_setup, monkeypatch):
    args, dev = install_setup
    args.unlink = False

    # Monkeypatch SW so its install returns success without contacting a device
    fake_sw = MagicMock()
    fake_sw.install.return_value = (True, "SW.install ok")
    monkeypatch.setattr(upgrade, "SW", lambda d: fake_sw)

    # Ensure CLI path is NOT called
    sentinel = {"called": False}

    def _should_not_call(*a, **k):
        sentinel["called"] = True
        return False, "should not be called"

    monkeypatch.setattr(upgrade, "_install_via_cli_with_unlink", _should_not_call)

    result = upgrade.install("test-host", dev)

    assert result["ok"] is True
    assert result["unlink_used"] is False
    assert sentinel["called"] is False
    assert fake_sw.install.called
