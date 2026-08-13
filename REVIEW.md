# Review rules for this repository

Review rules on top of the reviewer's default focus. Three things:
which findings are blocking here, which classes to report that the
default focus would otherwise skip, and which are noise. The reasoning
behind the rules lives in `.github/copilot-instructions.md` (its
numbered review-focus items are cited below) and `CLAUDE.md`, which the
reviewer also receives.

## Always blocking

- **Breaking the `display`-import boundary (§1).** A stray `print()` or
  debug write added to a core module (`upgrade.py`, `common.py`,
  `rsi.py`, `show.py`, `snapshot.py`), or a refactor that makes
  `display` reachable transitively from something those modules
  already export. `display.py` is the sole place that prints, precisely
  so a non-CLI caller can render result dicts via `display.format_*()`
  without ever touching stdout. Breaking it silently corrupts a
  downstream consumer's stdio channel — and **it will not show up as a
  test failure in this repository**, so judge it on the import and
  print structure, not on a green suite. The one deliberate exception
  is `common.get_targets()`'s inner `_fatal()`, which prints a
  diagnostic when `--json` is not set and then exits; that is not
  licence for new stdout writes elsewhere.
- **Moving `argcomplete` or `tqdm` to a module-level import (§1).**
  Both sit under `try`/`except ImportError` on purpose, mapping to the
  optional `completion` and `progress` extras, so a top-level import
  breaks the default extras-free install.
- **Routing the low-flash install path back through `SW.install()`
  (§2).** PyEZ does not expose the `unlink` option that
  EX2300/EX3400-class devices need to free boot flash during pkgadd,
  which is why `upgrade._install_via_cli_with_unlink()` bypasses it and
  shells out instead. A cleanup that removes that special case
  reintroduces "insufficient space" failures on real upgrades. A change
  touching `upgrade.py`'s install or `--unlink` logic must leave the
  path reachable and still covered by `tests/test_unlink.py`.
- **Host output written without `print_host_block` (§3).** `--workers
  N` runs devices in parallel through `run_parallel()`, so output has
  to be emitted as one atomic block per host under
  `display._print_lock`. A path that calls `print_host_header()` and
  then a separate print can have another host's output interleaved
  between them — that exact bug shipped in `cmd_facts` / `cmd_rsi` and
  was fixed in v0.23.1. Build a string and hand it to
  `print_host_block` instead.

## Report even though the default focus would not

- **A new code path that builds a CLI or RPC string from user input by
  ad-hoc formatting (§4).** `show COMMAND`, `config -f FILE` content
  and hostnames from `config.ini` or CLI args all reach a live device,
  and one layer up an MCP tool call.
  `_install_via_cli_with_unlink`'s `"request system software add %s
  unlink" % file_path` is the existing shape to compare against; prefer
  PyEZ's structured RPC or `dev.cli()` arguments where the choice
  exists. Report the malformed-input and injection risk concretely.
- **A `pyproject.toml` packaging-metadata change whose diff does not
  also account for `deb.yml` / `rpm.yml`**, as advisory. Those
  workflows build the `.deb` and `.rpm` from the sdist and wheel inside
  the release pipeline, outside the normal dev loop, so a metadata
  change can pass every check here and break packaging later. Judge it
  from the diff only — you receive changed files, so do not infer that
  an untouched workflow is wrong, just that the pull request should say
  it was considered.

## Never report

- **Generic MCP-server review patterns.** FastMCP setup, stdio JSON-RPC
  envelope framing, MCP tool schemas — none of that exists in this
  repository. Those concerns belong to the downstream wrapper project.
  A comment that would only make sense for an MCP server does not apply
  here.
- `common.get_targets()`'s `_fatal()` writing a diagnostic to stdout
  before exiting. That is the documented exception to the boundary
  rule above, not an oversight.
- Anything `ruff check .` already fails the build on. It is gated at a
  pinned version, so restating a finding costs a round trip and no
  information. This never applies to a rule listed under **Always
  blocking** above, even if a lint rule happens to fire on the same
  line.
- `ruff format` findings. Formatting is deliberately not gated here;
  see `ruff.toml`.
