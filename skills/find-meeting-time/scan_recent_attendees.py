#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""scan_recent_attendees.py — sweep recent Calendar events for small
meetings, extract (email, display name) pairs, and merge them into
people.yaml so the next time `find-meeting-time` proposes those people
the renderer can show full names without a fresh Glean lookup.

Why "small": large meetings (all-hands, town halls, distribution-list
invites) carry attendee lists that are mostly noise — people who don't
actually show up or who the requester doesn't interact with. The
default cap of 5 attendees keeps the cache focused on actual working
relationships. Bump with --max-attendees for fuller coverage.

Why "recent": Confluent's directory churns. A 30-day window catches
the people the user has interacted with lately without dragging in
stale entries from years ago.

Read-only Calendar scope (`calendar.events.readonly`); shares the
write token from create_event.py (which is a superset) so we don't
trigger a fresh OAuth consent.

Usage:
  uv run --script scan_recent_attendees.py
      [--since-days 30] [--max-attendees 5]
      [--out ~/.config/ai-seal-tools/find-meeting-time/people.yaml]
      [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import auth  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = auth.CREDENTIALS_DIR / "google_calendar_write_token.json"
DEFAULT_OUT = Path.home() / ".config" / "ai-seal-tools" / "find-meeting-time" / "people.yaml"


def list_events_in_window(svc, *, time_min: dt.datetime, time_max: dt.datetime, calendar_id: str = "primary") -> list[dict]:
    """Page through events.list across the window. Single-events-only so
    each recurring meeting expands to its concrete instances (which
    reflect the actual attendee list at that occurrence)."""
    events: list[dict] = []
    page_token = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def extract_attendees_from_event(event: dict, max_attendees: int) -> list[tuple[str, str | None]]:
    """Return (email_lower, displayName_or_None) for human attendees of an
    event with at most `max_attendees` people. Skips resources (rooms)
    and the all-day OOO-style events that have no attendee list. The
    organizer counts toward the attendee total even if not listed
    explicitly — Google often omits self from attendees on solo events.

    Returns empty list when the event should be excluded (too large,
    no attendees, all-day, declined-by-everyone, etc.)."""
    if event.get("status") == "cancelled":
        return []
    if event.get("eventType") in {"workingLocation", "focusTime", "outOfOffice"}:
        return []
    if event.get("start", {}).get("date"):
        # All-day events skew the data (PTO blocks, holidays etc.) — skip.
        return []

    raw_attendees = event.get("attendees") or []
    humans = [a for a in raw_attendees if not a.get("resource")]
    if not humans:
        return []
    if len(humans) > max_attendees:
        return []

    out: list[tuple[str, str | None]] = []
    for a in humans:
        email = (a.get("email") or "").strip().lower()
        if not email:
            continue
        name = (a.get("displayName") or "").strip() or None
        out.append((email, name))
    return out


def load_existing(path: Path) -> dict:
    """Read the on-disk people.yaml. Returns the parsed dict or an
    empty skeleton when the file's missing/empty. Never raises — a
    corrupt file gets logged + replaced (the original behavior of the
    record_*.py family)."""
    if not path.exists():
        return {"people": {}}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"[scan] warning: {path} is malformed ({e}); starting fresh", file=sys.stderr)
        return {"people": {}}
    if not isinstance(data, dict) or not isinstance(data.get("people"), dict):
        return {"people": data.get("people") if isinstance(data.get("people"), dict) else {}}
    return data


def merge_attendees(
    existing: dict,
    new_pairs: list[tuple[str, str | None]],
    *,
    event_date: str,
    requester_email: str | None,
) -> tuple[int, int]:
    """Merge (email, name) pairs into the existing people-dict in-place.
    Returns (added_count, updated_count). The requester's own email is
    excluded — we know who we are, no need to cache it. Entries with no
    displayName from Calendar are still recorded (with name=null) so a
    later Glean lookup can fill them in without re-scanning."""
    people = existing.setdefault("people", {})
    req = (requester_email or "").lower()
    added = 0
    updated = 0
    for email, name in new_pairs:
        if email == req:
            continue
        entry = people.get(email)
        if entry is None:
            people[email] = {
                "name": name,
                "last_seen": event_date,
                "sources": ["calendar-scan"],
            }
            added += 1
        else:
            # Update last_seen to the most recent event observation.
            old_last_seen = entry.get("last_seen", "")
            if event_date > old_last_seen:
                entry["last_seen"] = event_date
            # Fill in name if we now know it and didn't before, or
            # update if Calendar's displayName changed.
            if name and entry.get("name") != name:
                entry["name"] = name
                updated += 1
            sources = entry.setdefault("sources", [])
            if "calendar-scan" not in sources:
                sources.append("calendar-scan")
    return added, updated


def scan(
    *,
    since_days: int,
    max_attendees: int,
    out_path: Path,
    dry_run: bool,
    impersonate: str | None = None,
) -> dict:
    """End-to-end. Returns a summary dict suitable for printing."""
    now = dt.datetime.now(dt.timezone.utc)
    time_min = now - dt.timedelta(days=since_days)

    creds = auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=impersonate)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    # The authenticated user's primary-calendar id is their own email.
    # We pass it to merge_attendees so we don't cache ourselves.
    try:
        requester = svc.calendarList().get(calendarId="primary").execute().get("id")
    except HttpError:
        requester = None  # broader scope not available; harmless

    events = list_events_in_window(svc, time_min=time_min, time_max=now)
    existing = load_existing(out_path)

    events_used = 0
    total_added = 0
    total_updated = 0
    for ev in events:
        pairs = extract_attendees_from_event(ev, max_attendees=max_attendees)
        if not pairs:
            continue
        events_used += 1
        # Use the event's start date as last_seen (ISO YYYY-MM-DD).
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date") or ""
        event_date = start[:10] if start else now.date().isoformat()
        added, updated = merge_attendees(existing, pairs, event_date=event_date, requester_email=requester)
        total_added += added
        total_updated += updated

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml.safe_dump(existing, sort_keys=True, allow_unicode=True))

    return {
        "events_scanned": len(events),
        "events_used": events_used,
        "people_added": total_added,
        "people_updated": total_updated,
        "people_total": len(existing.get("people", {})),
        "since": time_min.date().isoformat(),
        "until": now.date().isoformat(),
        "out_path": str(out_path),
        "dry_run": dry_run,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-days", type=int, default=30)
    p.add_argument("--max-attendees", type=int, default=5)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--impersonate", help="Service-account DWD subject (rare)")
    p.add_argument("--dry-run", action="store_true", help="Read events but don't write the cache")
    args = p.parse_args()

    summary = scan(
        since_days=args.since_days,
        max_attendees=args.max_attendees,
        out_path=args.out,
        dry_run=args.dry_run,
        impersonate=args.impersonate,
    )
    import json
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
