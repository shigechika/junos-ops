#!/usr/bin/env python3
"""Add model-derived tags to config.ini by auto-detecting each host's JUNOS model.

Connects to every tagged host, fetches the product model via a single NETCONF
RPC (get-software-information, gather_facts=False), and appends the model name
as a new tag.  Hosts that already carry a non-role tag are skipped so the
script is safe to re-run.

Usage examples::

    # Dry-run: show what would change without writing
    python3 scripts/tag_models.py --config ~/.config/junos-ops/config.ini --dry-run

    # Apply to all tagged hosts
    python3 scripts/tag_models.py --config ~/.config/junos-ops/config.ini

    # Limit to a specific tag group
    python3 scripts/tag_models.py --config ~/.config/junos-ops/config.ini --tags main

    # Re-tag already-tagged hosts (e.g. after hardware replacement)
    python3 scripts/tag_models.py --config ~/.config/junos-ops/config.ini --force

    # Dry-run force re-tag for a specific group
    python3 scripts/tag_models.py --config ~/.config/junos-ops/config.ini --tags main --force --dry-run
"""

import argparse
import configparser
import os
import re
import sys
from pathlib import Path

from jnpr.junos import Device
from jnpr.junos.exception import ConnectError

# Tags that represent roles, not hardware models.  A host whose existing tags
# are a subset of ROLE_TAGS has not been model-tagged yet.
# Keep this set up-to-date when new role tags are added to config.ini;
# otherwise hosts with unknown role tags will be silently skipped.
ROLE_TAGS: frozenset[str] = frozenset({"main", "backup", "core", "ydc"})


def _fetch_model(section: str, cfg: configparser.ConfigParser) -> str | None:
    """Connect to *section* and return the product-model string, or None on error."""
    host = cfg.get(section, "host", fallback=section)
    kwargs = {
        "host": host,
        "port": int(cfg.get(section, "port", fallback="830")),
        "user": cfg.get(section, "id", fallback="admin"),
        "passwd": cfg.get(section, "pw", fallback=None),
        "ssh_private_key_file": os.path.expanduser(
            cfg.get(section, "sshkey", fallback="")
        ) or None,
        "gather_facts": False,
    }
    ssh_config = cfg.get(section, "ssh_config", fallback=None)
    if ssh_config:
        kwargs["ssh_config"] = os.path.expanduser(ssh_config)

    try:
        dev = Device(**kwargs)
        dev.open()
        try:
            xml = dev.rpc.get_software_information()
            model = (xml.findtext(".//product-model") or "").strip()
            return model or None
        finally:
            try:
                dev.close()
            except Exception:
                pass
    except ConnectError:
        return None
    except Exception as e:
        print(f"  {section}: unexpected error: {e}", file=sys.stderr)
        return None


def _patch_config(text: str, updates: dict[str, str]) -> str:
    """Return *text* with tags lines rewritten per *updates* {section: new_tags}."""
    lines = text.splitlines(keepends=True)
    result = []
    current_section: str | None = None
    for line in lines:
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            current_section = m.group(1)
        if current_section in updates and re.match(r"^tags\s*=", line):
            line = f"tags = {updates[current_section]}\n"
        result.append(line)
    return "".join(result)


def _existing_tags(cfg: configparser.ConfigParser, section: str) -> set[str]:
    raw = cfg.get(section, "tags", fallback="")
    return {t.strip() for t in raw.split(",") if t.strip()}


def _target_sections(
    cfg: configparser.ConfigParser,
    filter_tags: set[str] | None,
    *,
    force: bool = False,
) -> list[str]:
    sections = []
    for section in cfg.sections():
        if not cfg.has_option(section, "tags"):
            continue
        tags = _existing_tags(cfg, section)
        if filter_tags and not (tags & filter_tags):
            continue
        # Without --force: skip hosts that already have a non-role tag.
        if not force and tags - ROLE_TAGS:
            continue
        sections.append(section)
    return sections


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Add JUNOS model tags to config.ini hosts."
    )
    ap.add_argument(
        "--config",
        default="~/.config/junos-ops/config.ini",
        help="Path to config.ini (default: ~/.config/junos-ops/config.ini)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing to config.ini",
    )
    ap.add_argument(
        "--tags",
        help="Restrict to hosts carrying these tags (comma-separated, e.g. main,backup)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-tag already-tagged hosts too.  Replaces the existing model tag if it "
            "differs from the detected model; leaves it unchanged if it already matches."
        ),
    )
    args = ap.parse_args()

    config_path = Path(args.config).expanduser()
    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    filter_tags = (
        {t.strip() for t in args.tags.split(",") if t.strip()} if args.tags else None
    )
    targets = _target_sections(cfg, filter_tags, force=args.force)

    if not targets:
        if args.force:
            print("No hosts to process (none matched filter).", file=sys.stderr)
        else:
            print("No hosts to process (all already model-tagged, or none matched filter).", file=sys.stderr)
        return

    print(f"Targets: {len(targets)} host(s)", file=sys.stderr)

    updates: dict[str, str] = {}
    for section in targets:
        model = _fetch_model(section, cfg)
        if model is None:
            print(f"  {section}: FAILED", file=sys.stderr)
            continue

        raw = cfg.get(section, "tags", fallback="")
        current_list = [t.strip() for t in raw.split(",") if t.strip()]

        if args.force:
            # Rebuild tags as: existing role tags (in order) + detected model.
            role_tags = [t for t in current_list if t in ROLE_TAGS]
            existing_model_tags = {t for t in current_list if t not in ROLE_TAGS}
            if existing_model_tags == {model}:
                print(f"  {section}: {model} (unchanged)", file=sys.stderr)
                continue
            new_tags = ", ".join(role_tags + [model])
            action = f"replaced {existing_model_tags.pop()!r}" if existing_model_tags else "added"
            print(f"  {section}: {model} ({action})", file=sys.stderr)
        else:
            new_tags = ", ".join(current_list + [model])
            print(f"  {section}: {model}", file=sys.stderr)

        updates[section] = new_tags

    if not updates:
        print("Nothing to update.", file=sys.stderr)
        return

    if args.dry_run:
        print(f"\n[dry-run] {len(updates)} change(s) planned:")
        for section in sorted(updates):
            old = cfg.get(section, "tags", fallback="")
            print(f"  [{section}]  {old!r}  ->  {updates[section]!r}")
        return

    text = config_path.read_text()
    patched = _patch_config(text, updates)
    config_path.write_text(patched)
    print(f"\n{len(updates)} tag(s) updated in {config_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
