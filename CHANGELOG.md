# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
