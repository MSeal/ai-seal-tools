#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""get_event.py — fetch a Calendar event and print the same summary
shape as create_event.summarize_response.

Primary use: the hybrid booking path in SKILL.md, where
`create_event.py --conference none` creates the event via the API and
then Playwright clicks the Zoom add-on. After Playwright saves, this
script re-queries the event to confirm what conferenceData actually got
attached — fast, structured, no UI scraping. If Playwright failed to
attach conferencing (no Zoom URL in the result), the caller can decide
how to recover (manual click in the user's own browser tab is the
cheapest option, since the event already exists).

Shares the write token from create_event.py rather than asking for a
second OAuth consent — the calendar.events write scope is a superset
of read.

Usage:
  uv run --script get_event.py <event-id> \\
      [--requested-conference zoom|zoom-pool|meet|none] \\
      [--impersonate <user>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import auth  # noqa: E402
import create_event  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = auth.CREDENTIALS_DIR / "google_calendar_write_token.json"


def fetch_event(service, event_id: str, calendar_id: str = "primary") -> dict:
    """Single events.get call wrapped for testability — mock at this
    boundary rather than the discovery service."""
    return service.events().get(calendarId=calendar_id, eventId=event_id).execute()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("event_id", help="Calendar event ID (the `id` field, not the eid base64)")
    p.add_argument("--calendar", default="primary")
    p.add_argument(
        "--requested-conference",
        choices=["zoom", "zoom-pool", "meet", "none"],
        default="zoom",
        help="What conference was requested at create-time; controls "
             "the `conference_status` message in the output. Defaults "
             "to 'zoom' since this script is mostly invoked after the "
             "Playwright Zoom-attach step in the hybrid path.",
    )
    p.add_argument("--impersonate", help="Service-account DWD subject (rare)")
    args = p.parse_args()

    creds = auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=args.impersonate)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        event = fetch_event(svc, args.event_id, args.calendar)
    except HttpError as e:
        sys.exit(f"events.get failed: HTTP {e.resp.status}: {e}")

    json.dump(
        create_event.summarize_response(event, requested_conference=args.requested_conference),
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
