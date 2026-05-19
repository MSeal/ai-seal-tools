#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""record_person_name.py — write resolved email→name pairs into
people.yaml. Called by Claude in the find-meeting-time skill flow
after a Glean lookup fills in display names that Calendar's
attendees[].displayName didn't surface.

Two modes:

Single (after a Glean people lookup for one email):
  record_person_name.py single \\
      --email alice@example.com --name "Alice Example" \\
      --source glean

Bulk (after a batch Glean lookup; stdin = JSON object email→name,
where a name of null indicates "Glean couldn't find one"):
  echo '{"alice@example.com": "Alice Example", ...}' | \\
      record_person_name.py bulk --source glean

Updates last_seen only if the merged entry had no previous last_seen
date (a Glean enrichment doesn't tell us when the user last met the
person; that data point comes from scan_recent_attendees.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

DEFAULT_PATH = Path.home() / ".config" / "ai-seal-tools" / "find-meeting-time" / "people.yaml"


def load(path: Path) -> dict:
    """Read people.yaml or return a fresh skeleton. Tolerates a missing
    file, an empty file, or yaml with an empty `people:` block (which
    is what the committed template ships with)."""
    if not path.exists():
        return {"people": {}}
    text = path.read_text()
    if not text.strip():
        return {"people": {}}
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        return {"people": {}}
    if not isinstance(data.get("people"), dict):
        data["people"] = {}
    return data


def merge_name(
    data: dict,
    email: str,
    name: str | None,
    *,
    source: str,
    fetched_at: str,
) -> bool:
    """Merge one email→name pair into `data` in-place. Returns True iff
    the entry's name field changed (so the caller can count updates).
    A name of None or empty doesn't clobber an existing name — Glean
    might just not know."""
    email = email.strip().lower()
    if not email:
        return False
    people = data.setdefault("people", {})
    entry = people.get(email) or {}
    changed = False
    if name and entry.get("name") != name:
        entry["name"] = name
        changed = True
    sources = entry.setdefault("sources", [])
    if source not in sources:
        sources.append(source)
    entry.setdefault("name_fetched_at", fetched_at)
    if changed:
        entry["name_fetched_at"] = fetched_at
    people[email] = entry
    return changed


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", type=Path, default=DEFAULT_PATH)
    p.add_argument("--source", default="glean", help="Provenance tag stored in entry.sources")
    sub = p.add_subparsers(dest="mode", required=True)

    sp_single = sub.add_parser("single", help="Record one email→name pair")
    sp_single.add_argument("--email", required=True)
    sp_single.add_argument("--name", required=True)

    sub.add_parser("bulk", help="Read JSON object {email: name} from stdin")

    args = p.parse_args()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    data = load(args.path)

    updated = 0
    total = 0
    if args.mode == "single":
        total = 1
        if merge_name(data, args.email, args.name, source=args.source, fetched_at=fetched_at):
            updated += 1
    else:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"record_person_name: malformed JSON on stdin ({e})")
        if not isinstance(payload, dict):
            sys.exit("record_person_name: stdin JSON must be an object {email: name}")
        for email, name in payload.items():
            total += 1
            if merge_name(data, email, name, source=args.source, fetched_at=fetched_at):
                updated += 1

    save(args.path, data)
    print(json.dumps({
        "path": str(args.path),
        "input_count": total,
        "names_updated": updated,
        "people_total": len(data.get("people", {})),
    }, indent=2))


if __name__ == "__main__":
    main()
