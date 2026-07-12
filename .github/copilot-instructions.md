# Copilot review instructions for junos-ops

## Repository overview

`junos-ops` is a Python CLI/library that automates Juniper/JUNOS device
operations (model-aware upgrade, rollback, reboot, config push, RSI/SCF
collection) over NETCONF/SSH using `junos-eznc` (PyEZ). **This repository is
not an MCP server**: it has no `mcp` dependency, defines no MCP tools, and
runs no stdio JSON-RPC transport of its own.

It is, however, consumed as a library by a separate downstream project that
wraps its core functions as MCP tools. That downstream wrapper depends on an
architectural contract inside this repo (see "Review focus" below) to keep
its own stdio channel clean. Changes here that violate that contract are
cross-repo breaks, not just local style issues, even though this repo never
speaks MCP itself.

Module layout: `common.py` (config, connect, targeting, parallel exec),
`upgrade.py` / `show.py` / `snapshot.py` / `rsi.py` (core logic, all return
plain `dict`s), `display.py` (the only module that prints to stdout),
`cli.py` (argparse routing, `cmd_*` entry points).

## Build & validate

```bash
pip install -e ".[test]"
python3 -c "import junos_ops.cli"   # syntax/import smoke check
pytest tests/ -v --tb=short
pip install build && python -m build
```

This mirrors `.github/workflows/ci.yml` (Python 3.12/3.13 matrix). CI also
has separate `deb.yml`/`rpm.yml` jobs that package the sdist/wheel into
`.deb`/`.rpm`; a PR that changes `pyproject.toml` packaging metadata should
be checked against those workflows too, but they are not part of the normal
dev loop above.

## Review focus

### 1. The `display`-import boundary (do not regress)

Core modules (`upgrade.py`, `common.py`, `rsi.py`, `show.py`, `snapshot.py`)
return dicts and, on the normal path, do not write to stdout — `display.py`
is the *sole* place that prints. (The one deliberate exception is
`common.get_targets()`'s inner `_fatal()`, which prints a diagnostic to
`sys.stdout` when `--json` is not set and then `sys.exit(1)`; a library caller
that passes an empty host selection can hit it. Don't take that as licence to
add new stdout writes elsewhere in core modules.) Its own docstring states the
reason explicitly: non-CLI callers (i.e. the downstream MCP wrapper) use
`display.format_*()` to render result dicts as strings, precisely so they
never need to import or trigger anything that writes to stdout. As long as
that wrapper never imports `display`, it emits zero stdout output.

Relatedly, `argcomplete` (`cli.py`) and `tqdm` (in `_run_check_with_progress`)
are imported under `try/except ImportError` on purpose — they map to the
optional `completion` / `progress` extras, so a diff that moves either to a
module-level `import` would break the default (extras-free) install. Flag it.

This means: any stray `print()`/debug statement added to a core module, or
any refactor that makes `display` get imported transitively by something the
core modules already export, silently breaks the downstream consumer's
stdio channel. Flag this as a cross-repo regression, not a style nit — it
won't show up as a test failure in *this* repo.

### 2. Low-flash device install path (`--unlink`)

EX2300/EX3400-class devices have very small boot flash. PyEZ's `SW.install()`
does not expose the `unlink` option needed for `request system software add
... unlink` to free space during pkgadd, so major upgrades on these models
fail validation with "insufficient space". The workaround,
`upgrade._install_via_cli_with_unlink()`, bypasses `SW.install()` entirely
and shells out via `dev.cli("request system software add %s unlink" %
file_path)`. Treat this as a deliberate, load-bearing special case: a
"cleanup" refactor that routes low-flash installs back through the generic
`SW.install()` path silently reintroduces the space failure. Any change
touching `upgrade.py`'s install/`--unlink` logic should confirm this path
is still reachable and still tested (`tests/test_unlink.py`).

### 3. Atomic per-host output (`print_host_block`)

`--workers N` runs devices in parallel via `run_parallel()`. Output must be
emitted as one atomic block per host (`display.print_host_block()` /
`print_facts()`), guarded by `display._print_lock`. A subcommand that calls
`print_host_header()` and then a separate `print_*`/`print()` call can have
another host's output interleaved in between under concurrency — this exact
bug shipped in `cmd_facts`/`cmd_rsi` and was fixed in v0.23.1. Any new
subcommand or code path that writes host output directly (instead of
building a string and handing it to `print_host_block`) risks reintroducing
it.

### 4. Adversarial input on device-facing surfaces

Several inputs ultimately reach a live device and, one layer up, an MCP
tool call: `show COMMAND` (arbitrary string passed to `dev.cli()`),
`config -f FILE` (set-command file content committed to the device), and
hostnames resolved from `config.ini`/CLI args. Review any new code path that
builds a CLI/RPC string from user input the way `_install_via_cli_with_unlink`
does (`"request system software add %s unlink" % file_path`) for injection
or malformed-input risk, and prefer PyEZ's structured RPC/`dev.cli()` args
over ad-hoc string formatting where possible.

## Out of scope

Do not apply generic MCP-server review patterns here (FastMCP setup, stdio
JSON-RPC envelope framing, MCP tool schemas, etc.) — none of that exists in
this repository. Those concerns belong to the downstream wrapper project,
not to `junos-ops` itself.
