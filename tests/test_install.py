"""Tests for upgrade.install() — focused on branches not otherwise covered.

The existing test suite has no direct coverage of `install()` because it
is usually exercised end-to-end via `cmd_upgrade` or `process_host` (the
latter removed in v0.14.0). These tests drill into the less-travelled
paths inside `install()`:

1. The ``remote_check`` branch reached when `check_running_package` does
   not match and `pending` is ``None`` (the path that caused the v0.14.0
   regression where `install()` referenced the legacy
   `args.copy` / `args.update` / `args.install` attributes that were
   removed alongside `process_host`).
2. The ``install``-subcommand fail-fast when the remote package is
   missing.

Both tests also serve as regression guards: `mock_args` does **not**
set the legacy attributes, so if `install()` ever references them
again it will raise ``AttributeError`` here.
"""

from unittest.mock import MagicMock, patch


class TestInstallRemoteCheckBranch:
    """Coverage for the `install()` remote_check branch.

    This branch is reached only when:

    - ``args.force`` is False, AND
    - ``check_running_package`` returns ``match=False``, AND
    - ``get_pending_version`` returns ``None`` (so the compare/rollback
      block is skipped).

    Field engineering hosts like ``kudan-rt`` (QFX5110 running a newer
    version than the planning package file) routinely end up here.
    """

    def _setup_mocks(self, junos_upgrade, subcommand, dry_run):
        """Common mock setup for both parametrisations."""
        from junos_ops import common
        common.args.subcommand = subcommand
        common.args.dry_run = dry_run

        dev = MagicMock()
        dev.facts = {"model": "EX2300-24T", "hostname": "test-host"}
        return dev

    def test_upgrade_dry_run_skips_remote_check(
        self, junos_upgrade, mock_args, mock_config
    ):
        """``upgrade --dry-run`` records a skip step and proceeds.

        Regression guard for v0.14.0: before
        ``fix(install): use args.subcommand...``, this branch referenced
        ``common.args.copy`` / ``.update`` which no longer exist on the
        subcommand-era ``args`` namespace, raising ``AttributeError``.
        """
        dev = self._setup_mocks(junos_upgrade, subcommand="upgrade", dry_run=True)

        with (
            patch.object(
                junos_upgrade, "check_running_package",
                return_value={
                    "hostname": "test-host",
                    "running": "23.4R2-S6.6",
                    "expected_file": "jinstall-host-qfx-5e-x86-64-23.4R2-S4.11-secure-signed.tgz",
                    "match": False,
                },
            ),
            patch.object(junos_upgrade, "get_pending_version", return_value=None),
            patch.object(
                junos_upgrade, "copy",
                return_value={
                    "hostname": "test-host",
                    "ok": True,
                    "skipped": False,
                    "skip_reason": None,
                    "dry_run": True,
                    "steps": [],
                    "error": None,
                },
            ),
            patch.object(
                junos_upgrade, "clear_reboot",
                return_value={"ok": True, "dry_run": True, "message": "", "error": None},
            ),
        ):
            result = junos_upgrade.install("test-host", dev)

        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["error"] is None

        # The remote_check skip step must be present — this is the exact
        # path that raised AttributeError before the fix.
        remote_steps = [
            s for s in result["steps"] if s.get("action") == "remote_check"
        ]
        assert len(remote_steps) == 1
        assert "skip" in remote_steps[0]["message"]

        # And both the copy_result and sw_install step are recorded
        # further down the pipeline.
        assert result["copy_result"] is not None
        assert result["copy_result"]["ok"] is True
        sw_steps = [s for s in result["steps"] if s.get("action") == "sw_install"]
        assert len(sw_steps) == 1

    def test_install_only_fails_on_missing_remote(
        self, junos_upgrade, mock_args, mock_config
    ):
        """``install`` subcommand fails fast when remote package missing."""
        dev = self._setup_mocks(junos_upgrade, subcommand="install", dry_run=False)

        with (
            patch.object(
                junos_upgrade, "check_running_package",
                return_value={
                    "hostname": "test-host",
                    "running": "22.4R3-S6.5",
                    "expected_file": "junos-arm-32-22.4R3-S6.5.tgz",
                    "match": False,
                },
            ),
            patch.object(junos_upgrade, "get_pending_version", return_value=None),
            # Remote package check returns a "missing" dict.
            patch.object(
                junos_upgrade, "check_remote_package",
                return_value={
                    "hostname": "test-host",
                    "file": "junos-arm-32-22.4R3-S6.5.tgz",
                    "remote_path": "/var/tmp",
                    "algo": "md5",
                    "expected_hash": "abc",
                    "actual_hash": None,
                    "status": "missing",
                    "cached": False,
                    "message": "  - remote package: junos-arm-32-22.4R3-S6.5.tgz is not found.",
                    "error": None,
                },
            ),
        ):
            result = junos_upgrade.install("test-host", dev)

        assert result["ok"] is False
        assert result["error"] == "remote_missing"
        assert result["skip_reason"] == "remote_missing"

        remote_steps = [
            s for s in result["steps"] if s.get("action") == "remote_check"
        ]
        assert len(remote_steps) == 1
        assert "not found" in remote_steps[0]["message"]

    def test_no_legacy_args_attrs_on_namespace(self, mock_args):
        """Explicit guard: mock_args must not set the removed legacy flags.

        If these attributes come back (e.g. someone re-adds them to
        `conftest.py` for a test), the regression guard tests above
        would silently stop protecting against the original bug. This
        test will fail if any legacy flag is re-introduced.
        """
        for legacy in ("copy", "update", "install", "showversion", "rollback"):
            assert not hasattr(mock_args, legacy), (
                f"mock_args.{legacy} was removed in v0.14.0 with process_host; "
                "reintroducing it would mask the install() regression guard."
            )
