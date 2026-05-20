#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""move_event.py — reschedule an existing Calendar event to a new time slot.

The natural follow-up to freebusy.py + render_slot.py when the user already
has the meeting on the books and just wants to shift it: same attendees,
same Zoom link, same description, new start/end. Avoids the
delete-and-recreate dance (which would issue cancellation + creation
emails, churn the event ID in third-party integrations, and lose any
attendee RSVPs).

Uses events.patch with sendUpdates="all" so attendees get the standard
"event time changed" notification. Patching only `start` and `end` preserves
attendees, conferenceData, location, description, and the recurrence rule
where applicable — anything you don't pass is left alone server-side.

Scope: shares the calendar.events write scope and cached token with
create_event.py, so the first move after a fresh upgrade from a
read-only token will pop a browser; subsequent calls reuse the cached
write token.

Usage:
  uv run --script move_event.py <event-id> \\
      --start 2026-05-27T13:00:00-07:00 \\
      --end   2026-05-27T13:30:00-07:00 \\
      [--calendar primary] \\
      [--send-updates all|externalOnly|none] \\
      [--impersonate <user>] \\
      [--dry-run]

Output: same JSON shape as create_event.summarize_response — event_id,
html_link, join_url, conference summary, attendees, new start/end —
so the skill's user-facing rendering after a move matches the rendering
after a fresh booking.
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
import create_event  # noqa: E402  — reuse summarize_response for output parity

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = auth.CREDENTIALS_DIR / "google_calendar_write_token.json"

VALID_SEND_UPDATES = {"all", "externalOnly", "none"}


def build_patch_body(start: dt.datetime, end: dt.datetime) -> dict:
    """Pure function: minimal events.patch body that only shifts time.

    Intentionally narrow — only `start` and `end`. Anything else passed
    here would silently overwrite server-side state the caller didn't
    intend to touch (most notably `attendees`, which would *replace* the
    list and drop people's existing RSVPs)."""
    return {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def patch_event(
    svc,
    event_id: str,
    body: dict,
    *,
    calendar_id: str = "primary",
    send_updates: str = "all",
) -> dict:
    """Execute the patch and return the updated event dict.

    Wrapped so tests can swap `svc` for a mock at the events().patch()
    boundary (the same shape tests/test_sheets_writer.py uses for sheets)."""
    return svc.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=body,
        sendUpdates=send_updates,
    ).execute()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("event_id", help="Calendar event ID (the `id` field from get_event.py / create_event.py)")
    p.add_argument("--start", required=True, help="New start, ISO 8601 with offset")
    p.add_argument("--end", required=True, help="New end, ISO 8601 with offset")
    p.add_argument("--calendar", default="primary", help="Calendar that owns the event (default: primary)")
    p.add_argument(
        "--send-updates",
        choices=sorted(VALID_SEND_UPDATES),
        default="all",
        help="Who gets the reschedule email. Default 'all' matches create_event.py.",
    )
    p.add_argument("--impersonate", help="Service-account DWD subject (rare)")
    p.add_argument("--dry-run", action="store_true", help="Print the patch body without calling the API")
    args = p.parse_args()

    start = dt.datetime.fromisoformat(args.start).astimezone()
    end = dt.datetime.fromisoformat(args.end).astimezone()
    if end <= start:
        sys.exit(f"--end ({end.isoformat()}) must be after --start ({start.isoformat()})")

    body = build_patch_body(start, end)

    if args.dry_run:
        print(json.dumps({
            "would_patch": {
                "calendarId": args.calendar,
                "eventId": args.event_id,
                "body": body,
                "sendUpdates": args.send_updates,
            }
        }, indent=2))
        return

    creds = auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=args.impersonate)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        event = patch_event(
            svc, args.event_id, body,
            calendar_id=args.calendar,
            send_updates=args.send_updates,
        )
    except HttpError as e:
        sys.exit(f"events.patch failed: HTTP {e.resp.status}: {e}")

    requested_conf = "zoom" if (event.get("conferenceData") or {}).get("entryPoints") else "none"
    json.dump(create_event.summarize_response(event, requested_conference=requested_conf), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
