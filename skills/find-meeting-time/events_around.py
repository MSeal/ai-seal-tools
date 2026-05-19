#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""events_around.py — fetch a single attendee's own events in a
window padded around a slot, so render_slot.py can show the
requester's day for ±N hours of context.

Output JSON: a list of event dicts shaped like freebusy.py's `conflict`
inner field (visible/summary/category/movability/conflict_start/end).
Reuses the freebusy classifier so glyph selection is consistent
between the group slot timeline and the context timeline.

Usage (single slot, requester only):
  events_around.py \\
      --slot-start 2026-05-22T14:30:00-07:00 \\
      --slot-end   2026-05-22T15:15:00-07:00 \\
      --email      mseal@confluent.io \\
      [--hours-before 2 --hours-after 2]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import auth  # noqa: E402
import freebusy  # noqa: E402  — reuse classify_event for glyph parity

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = auth.CREDENTIALS_DIR / "google_calendar_write_token.json"


def fetch_events_in_window(
    svc,
    *,
    calendar_id: str,
    time_min: dt.datetime,
    time_max: dt.datetime,
) -> list[dict]:
    """Single events.list call, paged. singleEvents=True so recurring
    masters expand to instances (we want the actual event at this
    moment, not the recurrence rule)."""
    items: list[dict] = []
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
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def classify_and_shape(event: dict) -> dict | None:
    """Convert a raw Calendar event into the shape render_slot.py
    expects in `slot["conflicts"][i]["conflict"]`. Returns None if
    the event shouldn't render as a context conflict (cancelled,
    transparent, declined-by-self, working-location pseudo-event,
    all-day block). Reuses freebusy.classify_event + event_blocks_time
    so the context timeline's glyphs line up with the group timeline's
    for the same event."""
    classification = freebusy.classify_event(event)
    if classification["is_all_day"]:
        # All-day blocks (PTO, holidays) skew a per-hour visualization.
        return None
    if not freebusy.event_blocks_time(event, classification):
        return None
    start_iso = event.get("start", {}).get("dateTime")
    end_iso = event.get("end", {}).get("dateTime")
    if not start_iso or not end_iso:
        return None
    return {
        "visible": classification["visible"],
        "summary": classification["summary"] or "(no title)",
        "category": classification["category"],
        "movability": classification["movability"],
        "recurring": classification["recurring"],
        "is_all_day": False,
        "status": classification["status"],
        "conflict_start": start_iso,
        "conflict_end": end_iso,
    }


def events_around(
    svc,
    *,
    calendar_id: str,
    slot_start: dt.datetime,
    slot_end: dt.datetime,
    hours_before: float,
    hours_after: float,
) -> list[dict]:
    """Window = [slot_start − hours_before, slot_end + hours_after]."""
    time_min = slot_start - dt.timedelta(hours=hours_before)
    time_max = slot_end + dt.timedelta(hours=hours_after)
    raw = fetch_events_in_window(svc, calendar_id=calendar_id, time_min=time_min, time_max=time_max)
    shaped: list[dict] = []
    for ev in raw:
        s = classify_and_shape(ev)
        if s:
            shaped.append(s)
    return shaped


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slot-start", required=True, help="ISO 8601 with offset")
    p.add_argument("--slot-end", required=True, help="ISO 8601 with offset")
    p.add_argument("--email", required=True, help="Calendar id (typically the requester's email)")
    p.add_argument("--hours-before", type=float, default=2.0)
    p.add_argument("--hours-after", type=float, default=2.0)
    p.add_argument("--impersonate", help="Service-account DWD subject (rare)")
    args = p.parse_args()

    slot_start = dt.datetime.fromisoformat(args.slot_start)
    slot_end = dt.datetime.fromisoformat(args.slot_end)

    creds = auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=args.impersonate)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        events = events_around(
            svc,
            calendar_id=args.email,
            slot_start=slot_start,
            slot_end=slot_end,
            hours_before=args.hours_before,
            hours_after=args.hours_after,
        )
    except HttpError as e:
        sys.exit(f"events.list failed: HTTP {e.resp.status}: {e}")

    print(json.dumps({
        "email": args.email,
        "slot_start": slot_start.isoformat(),
        "slot_end": slot_end.isoformat(),
        "hours_before": args.hours_before,
        "hours_after": args.hours_after,
        "context_events": events,
    }, indent=2))


if __name__ == "__main__":
    main()
