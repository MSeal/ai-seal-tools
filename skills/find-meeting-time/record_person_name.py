#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""record_person_name.py — write resolved email→{name,location,timezone}
pairs into people.yaml. Called by Claude in the find-meeting-time skill
flow after a Glean lookup fills in display names and locations that
Calendar's attendees[].displayName / freebusy data didn't surface.

Two modes:

Single (after a Glean people lookup for one email):
  record_person_name.py single \\
      --email alice@example.com --name "Alice Example" \\
      --location "GB Remote United Kingdom" \\
      [--timezone Europe/London]   # override the inferred TZ
      --source glean

  Either --name or --location can be omitted to update only the field
  the caller has. --timezone, if not passed, is inferred from --location
  via timezone_map.infer_timezone.

Bulk (after a batch Glean lookup):

  Old shape (name-only, backward compat):
    {"alice@example.com": "Alice Example", ...}

  New shape (rich per-person dicts):
    {"alice@example.com": {"name": "Alice Example",
                            "location": "GB Remote United Kingdom",
                            "timezone": "Europe/London"}, ...}

  Both shapes can be mixed in the same payload.

  echo '<json>' | record_person_name.py bulk --source glean

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

from timezone_map import infer_timezone

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


def merge_person(
    data: dict,
    email: str,
    name: str | None = None,
    *,
    location: str | None = None,
    timezone: str | None = None,
    source: str,
    fetched_at: str,
) -> bool:
    """Merge one email→person record into `data` in-place.

    Returns True iff any persisted field changed. None-valued args
    don't clobber an existing field — Glean might just not know that
    bit yet (e.g., name resolved, location still pending).

    If `location` is provided and `timezone` is not, the TZ is inferred
    via timezone_map.infer_timezone and stored alongside the raw
    location string."""
    email = email.strip().lower()
    if not email:
        return False
    people = data.setdefault("people", {})
    entry = people.get(email) or {}
    changed = False

    if name and entry.get("name") != name:
        entry["name"] = name
        entry["name_fetched_at"] = fetched_at
        changed = True
    entry.setdefault("name_fetched_at", fetched_at)

    if location and entry.get("location") != location:
        entry["location"] = location
        entry["location_fetched_at"] = fetched_at
        changed = True

    resolved_tz = timezone or (infer_timezone(location) if location else None)
    if resolved_tz and entry.get("timezone") != resolved_tz:
        entry["timezone"] = resolved_tz
        changed = True

    sources = entry.setdefault("sources", [])
    if source not in sources:
        sources.append(source)

    people[email] = entry
    return changed


# Backward-compat alias — older callers / tests use merge_name.
def merge_name(
    data: dict,
    email: str,
    name: str | None,
    *,
    source: str,
    fetched_at: str,
) -> bool:
    """Deprecated alias; prefer merge_person."""
    return merge_person(data, email, name, source=source, fetched_at=fetched_at)


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))


def _normalize_bulk_value(value) -> dict:
    """Bulk JSON values can be a bare name string (legacy) or a dict
    with name/location/timezone. Normalize to a dict shape so the
    caller can unpack uniformly."""
    if value is None:
        return {"name": None}
    if isinstance(value, str):
        return {"name": value}
    if isinstance(value, dict):
        return {
            "name": value.get("name"),
            "location": value.get("location"),
            "timezone": value.get("timezone"),
        }
    raise ValueError(f"unsupported bulk value type: {type(value).__name__}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", type=Path, default=DEFAULT_PATH)
    p.add_argument("--source", default="glean", help="Provenance tag stored in entry.sources")
    sub = p.add_subparsers(dest="mode", required=True)

    sp_single = sub.add_parser("single", help="Record one email→person record")
    sp_single.add_argument("--email", required=True)
    sp_single.add_argument("--name", default=None,
                           help="Display name. Omit to update only location/timezone.")
    sp_single.add_argument("--location", default=None,
                           help="Raw Glean location string, e.g. 'GB Remote United Kingdom'.")
    sp_single.add_argument("--timezone", default=None,
                           help="IANA timezone (e.g. 'Europe/London'). Overrides inference from --location.")

    sub.add_parser("bulk", help="Read JSON object {email: name|record} from stdin")

    args = p.parse_args()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    data = load(args.path)

    updated = 0
    total = 0
    if args.mode == "single":
        if not (args.name or args.location or args.timezone):
            sys.exit("record_person_name: single mode requires at least one of --name, --location, --timezone")
        total = 1
        if merge_person(
            data, args.email, args.name,
            location=args.location, timezone=args.timezone,
            source=args.source, fetched_at=fetched_at,
        ):
            updated += 1
    else:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"record_person_name: malformed JSON on stdin ({e})")
        if not isinstance(payload, dict):
            sys.exit("record_person_name: stdin JSON must be an object {email: name|record}")
        for email, value in payload.items():
            total += 1
            try:
                fields = _normalize_bulk_value(value)
            except ValueError as e:
                sys.exit(f"record_person_name: bad entry for {email!r}: {e}")
            if merge_person(
                data, email, fields.get("name"),
                location=fields.get("location"),
                timezone=fields.get("timezone"),
                source=args.source, fetched_at=fetched_at,
            ):
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
