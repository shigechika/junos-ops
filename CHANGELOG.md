# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.15.0] - 2026-04-14

### Added
- **`check` subcommand ([#41](https://github.com/shigechika/junos-ops/issues/41)): unified pre-flight verification across NETCONF reachability and firmware hash.** Sub-flags: `--connect` (NETCONF + facts probe), `--local` (local firmware checksum, no NETCONF required), `--remote` (device-side checksum, confirms SCP copy completed), `--all` (shorthand for all three). Output is an aligned table with one row per host; exit code is non-zero if any host has `fail` / `bad` / `missing` / `error`. Default (no flag) is `--connect` — fills the previously-awkward "just verify reachability" gap.
  - `--model MODEL` override, or optional `model = MX5-T` key per host in `config.ini`, so `--local` works entirely offline against a staging server.
  - Model resolution order: `--model` > `config.ini` `[host].model` > `dev.facts["model"]` (only when connected).
- `upgrade.check_local_package_by_model(hostname, model)` — device-less local checksum helper. Uses `hashlib` directly (no PyEZ `SW` dependency). The existing `check_local_package(hostname, dev)` is now a thin wrapper that resolves the model from `dev.facts` and delegates to this new core.
- `upgrade.check_remote_package_by_model(hostname, dev, model)` — companion by-model variant for remote checks (same semantics, model supplied by caller).
- `display.format_check_table(rows, *, show_connect, show_local, show_remote)` / `print_check_table(...)` — table renderer shared by the CLI and any non-CLI caller (e.g. future junos-mcp tool).

## [0.14.1] - 2026-04-05

### Changed
- **Eliminated the last remaining `print()` calls in the core ([#40](https://github.com/shigechika/junos-ops/issues/40)).** Migrated `check_local_package()`, `check_remote_package()`, and `clear_reboot()` to dict returns matching the 0.14.0 refactor convention. With this change, every `junos_ops.upgrade` and `junos_ops.rsi` core function is guaranteed print-free, fully closing the MCP STDIO corruption motivation behind #40.
  - `check_local_package()` returns `{hostname, file, local_file, algo, expected_hash, actual_hash, status, cached, message, error}` where `status` is one of `"ok"` / `"bad"` / `"missing"` / `"error"` / `"unchecked"`.
  - `check_remote_package()` returns the same shape with `remote_path` in place of `local_file`.
  - `clear_reboot()` returns `{ok, dry_run, message, error}` (same shape as `delete_snapshots()`).
- **`show_version()` / `dry_run()` schemas upgraded.** The `local_package` and `remote_package` fields are now nested dicts (previously bools); display layer reads their `message` field to render the legacy one-liner output.
- **`junos_ops.display` adds a `format_*(result) -> str` API.** Every `print_*` function now delegates to a `format_*` counterpart that returns the rendered text without touching stdout. Non-CLI callers (e.g. `junos-mcp`) can build response strings directly from the dict + `format_*` without needing `contextlib.redirect_stdout`.
- `get_pending_version()` error-path `print()` calls replaced with `logger.error()` (non-breaking; return value unchanged).

### Fixed
- `install()` correctly appends `clear_reboot` / `remote_check` steps to its result dict instead of relying on the now-gone direct prints from the helpers.

## [0.14.0] - 2026-04-05

### Changed
- **Core refactor ([#40](https://github.com/shigechika/junos-ops/issues/40)): all core functions now return dicts and no longer print to stdout.** Human-readable output is produced by the new `junos_ops.display` module that consumes those dicts. Non-CLI consumers (e.g. `junos-mcp`) can opt out of stdout writes entirely by not importing `display`.
- `common.connect()` now returns `{hostname, host, ok, dev, error, error_message}` (previously `(err, dev)` tuple).
- `common.read_config()` now returns `{ok, path, sections, error}` (previously a bool).
- `upgrade.copy()`, `install()`, `rollback()`, `reboot()`, `load_config()`, `list_remote_path()`, `dry_run()`, `delete_snapshots()`, `check_running_package()`, `check_and_reinstall()` all return structured dicts. `copy`/`install`/`reboot`/`load_config`/`check_and_reinstall` expose a `steps` list for per-action progress. `reboot` preserves the legacy 0..6 exit codes in `result["code"]`.
- `rsi.collect_rsi()` added as the dict-returning core; `cmd_rsi` is now a thin CLI wrapper. `get_support_information()` also returns a dict.
- `load_config()` emits `logger.info` per step so operators watching logs still see real-time progress.
- `display` layer prints are serialised through a module-level lock so `--workers N` produces readable interleaved output.

### Removed
- `cli.process_host()` and its backward-compat function aliases (`cli.copy`, `cli.install`, etc.) — dead code from the pre-subcommand CLI era. CLI dispatches directly to `cmd_*` functions.
- `tests/test_process_host.py` (covered the removed code path).

### Fixed
- `upgrade.install()` referenced the legacy `args.copy` / `args.update` / `args.install` attributes in its `remote_check` branch, which were removed alongside `process_host`. Switched the branch selection to `common.args.subcommand`. Found during the v0.14.0 smoke test on a QFX5110 where `check_running_package` did not match (running newer than the planning package) and `pending` was None, driving `install()` into this previously untested code path. Added `tests/test_install.py` as a regression guard.

## [0.13.0] - 2026-03-14

### Added
- `--health-check uptime`: NETCONF RPC probe using `get-system-uptime-information` — verifies device responsiveness without ICMP/ping dependency
- Jinja2 template support for `config` subcommand ([#30](https://github.com/shigechika/junos-ops/issues/30)): use `.j2` files to generate per-host set commands from a single template
  - Template variables from `var_*` keys in config.ini (DEFAULT inheritance supported), device facts (`facts.*`), and `hostname`
  - `StrictUndefined` mode for safety — undefined variables cause immediate error
  - Jinja2 as optional dependency: `pip install junos-ops[template]`
- Documentation: `docs/config.md` and `docs/template.md` with English/Japanese versions

### Changed
- Config push workflow documentation extracted from README to `docs/config.md` for better organization
- Documentation IPs updated to RFC 5737 addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`)

## [0.12.0] - 2026-02-26

### Added
- `lpath` config option: local firmware directory path for upgrade/copy operations (e.g., `lpath = ~/firmware`). Supports `~` expansion. When set, package files are resolved as `lpath/filename` instead of requiring CWD or absolute paths. Per-host override supported.
- `RSI_DIR` now supports `~` expansion (e.g., `RSI_DIR = ~/rsi/`), consistent with `sshkey` and `lpath`

## [0.11.2] - 2026-02-25

### Changed
- `--health-check` now accepts multiple commands (repeatable). Commands are tried in order — passes if any succeeds, fails only if all fail. Enables fallback health checks for heterogeneous environments.
- `--no-health-check` is now an independent flag (no longer mutually exclusive with `--health-check`)
- Documentation: replaced private/non-routable IPs with RFC 5737 `192.0.2.1` in examples

## [0.11.1] - 2026-02-24

### Changed
- Default RPC timeout for `config` subcommand changed from 30s (PyEZ default) to 120s — commit confirmed needs at least 60s to complete

## [0.11.0] - 2026-02-24

### Added
- `config --timeout N` option: override RPC timeout in seconds for slow devices ([#39](https://github.com/shigechika/junos-ops/issues/39)). Also supports `timeout` setting in config.ini (CLI option takes precedence).
- `config --no-confirm` option: skip commit confirmed and health check, commit directly. Useful for devices where commit confirmed is too slow (e.g., SRX3xx series).

## [0.10.1] - 2026-02-24

### Changed
- Default health check target changed from `8.8.8.8` to `255.255.255.255` — works in any network environment without requiring internet access

## [0.10.0] - 2026-02-24

### Added
- `show --retry N` option: retry on `RpcTimeoutError` with incremental backoff (5s, 10s, 15s, ...) ([#38](https://github.com/shigechika/junos-ops/issues/38))

## [0.9.2] - 2026-02-24

### Fixed
- `junos-ops version` no longer fails with ERROR when upgrade `.file` option is not defined in config.ini ([#37](https://github.com/shigechika/junos-ops/issues/37))

## [0.9.1] - 2026-02-24

### Fixed
- `junos-ops -c accounts.ini` without a subcommand no longer fails with "invalid choice" error ([#36](https://github.com/shigechika/junos-ops/issues/36))

### Added
- Homebrew tap support (`brew install shigechika/tap/junos-ops`)

## [0.9.0] - 2026-02-21

### Added
- `--tags` option: filter target hosts by tags defined in config.ini (comma-separated, AND match). Supports union with explicit hostnames and case-insensitive matching.

## [0.8.0] - 2026-02-21

### Added
- `config --health-check` / `--no-health-check`: run a health check command (default: `ping count 3 8.8.8.8 rapid`) between `commit confirmed` and the final `commit`. On failure, the final commit is withheld and JUNOS auto-rolls back when the timer expires.

## [0.7.0] - 2026-02-21

### Added
- `show -f` option: run multiple CLI commands from a file in a single NETCONF session
  (`junos-ops show -f commands.txt -c config.ini`)

### Fixed
- `config -f`: strip `#` comment lines and blank lines before sending to PyEZ, preventing `ConfigLoadError: unknown command`

## [0.6.2] - 2026-02-20

### Changed
- Upgrade and RSI workflow diagrams changed from `flowchart LR` to `flowchart TD` for better readability

## [0.6.1] - 2026-02-19

### Added
- Mermaid workflow diagrams in README (CLI architecture, upgrade workflow, upgrade internal flow, reboot safety flow, config push workflow)
- Explanatory text for all workflow diagrams describing the purpose of each safety mechanism

## [0.6.0] - 2026-02-17

### Added
- `show` subcommand: run arbitrary CLI commands across devices in parallel
  (`junos-ops show "show bgp summary" -c config.ini --workers 10`)
- argcomplete tab completion (optional dependency)

## [0.5.3] - 2025-05-24

### Changed
- Standardized all docstrings to English

## [0.5.2] - 2025-05-24

### Fixed
- Changed README language switch links to absolute URLs for PyPI compatibility

## [0.5.1] - 2025-05-23

### Changed
- Updated install instructions to use PyPI (`pip install junos-ops`)

### Added
- PyPI release workflow (GitHub Actions)

## [0.5] - 2025-05-23

### Added
- Subcommand-based CLI architecture (`upgrade`, `copy`, `install`, `rollback`, `version`, `reboot`, `ls`, `config`, `rsi`)
- `config` subcommand: push set-format command files with commit confirmed safety
- `rsi` subcommand: parallel RSI/SCF collection
- `DISPLAY_STYLE` setting to customize SCF output format
- `delete_snapshots()` for EX/QFX series disk space management
- Automatic reinstall on config change detection during reboot
- `logging.ini` support with XDG config path search
- Parallel execution support (`--workers`)
- pip-installable package with `pyproject.toml`
- CI with GitHub Actions (Python 3.12/3.13 matrix)
- Comprehensive test suite (100 tests)

### Changed
- Refactored from single-file script to modular package (`junos_ops/`)
- Version managed in `junos_ops/__init__.py`

## [0.1] - 2022-12-01

### Added
- Initial release
- Device model auto-detection and package mapping
- SCP transfer with checksum verification
- Package install, rollback, and reboot scheduling
- INI-based configuration

[0.12.0]: https://github.com/shigechika/junos-ops/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/shigechika/junos-ops/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/shigechika/junos-ops/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/shigechika/junos-ops/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/shigechika/junos-ops/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/shigechika/junos-ops/compare/v0.9.2...v0.10.0
[0.9.2]: https://github.com/shigechika/junos-ops/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/shigechika/junos-ops/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/shigechika/junos-ops/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/shigechika/junos-ops/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/shigechika/junos-ops/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/shigechika/junos-ops/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/shigechika/junos-ops/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/shigechika/junos-ops/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/shigechika/junos-ops/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/shigechika/junos-ops/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/shigechika/junos-ops/compare/v0.5...v0.5.1
[0.5]: https://github.com/shigechika/junos-ops/compare/0.1...v0.5
[0.1]: https://github.com/shigechika/junos-ops/releases/tag/0.1
