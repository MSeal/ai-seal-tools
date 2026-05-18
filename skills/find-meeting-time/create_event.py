#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""create_event.py — book a slot returned by freebusy.py.

Creates a Google Calendar event on the requester's primary calendar with
the given attendees and (by default) a Zoom link via the Workspace add-on.
Falls back to Google Meet if the user configures `default_conference: meet`
in config.yaml, or to no conference link with `none`.

Scope: requires `https://www.googleapis.com/auth/calendar.events` (write).
This is a SUPERSET of freebusy.py's `calendar.events.readonly` scope, so
the cached token from freebusy won't satisfy this script — the auth path's
scope-mismatch detector will spot that and pop a browser for fresh
consent on first run. The new token is cached separately at
~/.config/ai-seal-tools/credentials/google_calendar_write_token.json so
freebusy.py's read-only token doesn't get widened.

Usage:
  uv run --script create_event.py \\
      --start 2026-05-18T13:30:00-07:00 \\
      --end   2026-05-18T14:00:00-07:00 \\
      --summary "Quick chat: eve + frank" \\
      --attendees eve@example.com,frank@example.com \\
      [--description "..."] \\
      [--conference zoom|meet|none]   # default from config.yaml

Output: JSON with event_id, html_link, conference summary (entry points
+ which solution dispatched). If the requested conference type didn't
attach an entry point (e.g., Zoom add-on not installed), the JSON's
`conference_status` flags that so Claude can suggest retrying with
`--conference meet`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import auth  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = auth.CREDENTIALS_DIR / "google_calendar_write_token.json"
CONFIG_FILE = auth.CONFIG_DIR / "find-meeting-time" / "config.yaml"

VALID_CONFERENCE_TYPES = {"zoom", "zoom-pool", "meet", "none"}


def load_default_conference(path: Path = CONFIG_FILE) -> str:
    """Read `default_conference` from config.yaml. Returns one of
    VALID_CONFERENCE_TYPES. Falls back to 'zoom' when unset."""
    if not path.exists():
        return "zoom"
    data = yaml.safe_load(path.read_text()) or {}
    val = str(data.get("default_conference") or "zoom").strip().lower()
    if val not in VALID_CONFERENCE_TYPES:
        print(f"[create_event] unknown default_conference {val!r}; using 'zoom'", file=sys.stderr)
        return "zoom"
    return val


def load_zoom_personal_url(path: Path = CONFIG_FILE) -> str | None:
    """Read `zoom_personal_meeting_url` from config.yaml."""
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    return (data.get("zoom_personal_meeting_url") or "").strip() or None


def load_zoom_fallback_rooms(path: Path = CONFIG_FILE) -> list[str]:
    """Read `zoom_fallback_rooms` from config.yaml. Returns list of URLs,
    empty if unset. Used by --conference zoom-pool to rotate when the
    personal room would conflict (back-to-back meetings)."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    rooms = data.get("zoom_fallback_rooms") or []
    if not isinstance(rooms, list):
        return []
    return [str(r).strip() for r in rooms if str(r).strip()]


def pick_zoom_url(
    conference: str,
    start: dt.datetime,
    *,
    override: str | None,
    personal: str | None,
    fallback: list[str],
) -> str:
    """Resolve the Zoom URL to attach. Priority:
      1. --zoom-url override if set
      2. zoom-pool → deterministic hash(start) pick from fallback
      3. zoom → personal_meeting_url
    Raises ValueError with a clear message if the selected source isn't
    configured. Deterministic pool selection means a given event slot
    always lands on the same fallback room (helpful for debugging)."""
    if override:
        return override
    if conference == "zoom-pool":
        if not fallback:
            raise ValueError(
                "--conference zoom-pool selected but zoom_fallback_rooms is "
                "empty in config.yaml. Set the list or pass --zoom-url, "
                "or fall back to --conference zoom (personal room)."
            )
        # Deterministic rotation by start time — same slot always picks
        # the same room, so re-running create on the same slot is stable.
        idx = abs(hash(start.isoformat())) % len(fallback)
        return fallback[idx]
    # conference == "zoom"
    if not personal:
        raise ValueError(
            "--conference zoom selected but zoom_personal_meeting_url is "
            "not set in config.yaml. Either set it (recommended for casual "
            "internal meetings), pass --zoom-url, switch to --conference "
            "zoom-pool, or use --conference meet."
        )
    return personal


def build_event_body(
    start: dt.datetime,
    end: dt.datetime,
    summary: str,
    attendees: list[str],
    *,
    description: str = "",
    conference: str = "zoom",
    zoom_url: str | None = None,
) -> dict:
    """Pure function: construct the `events.insert` request body.

    `conference` selects how the conference link is attached:
      - 'zoom' / 'zoom-pool': hand-crafted conferenceData carrying the
        supplied `zoom_url`. Google's API doesn't let us trigger the
        Zoom Workspace add-on programmatically (see the SETUP/SKILL
        docs for why), but the UI happily renders any
        conferenceData.entryPoints we provide as a join button.
      - 'meet': createRequest with conferenceSolutionKey.type=hangoutsMeet
        — Google mints a Meet link server-side.
      - 'none': bare event, no conferenceData.
    """
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "attendees": [{"email": e.strip().lower()} for e in attendees if e.strip()],
    }
    if description:
        body["description"] = description
    if conference == "meet":
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    elif conference in {"zoom", "zoom-pool"}:
        if not zoom_url:
            raise ValueError("zoom_url must be supplied for zoom conferences (pick_zoom_url first)")
        body["conferenceData"] = {
            "conferenceSolution": {
                "key": {"type": "addOn"},
                "name": "Zoom Meeting",
            },
            "entryPoints": [{
                "entryPointType": "video",
                "uri": zoom_url,
                "label": "Join Zoom Meeting",
            }],
            "conferenceId": str(uuid.uuid4()),
        }
    return body


def summarize_response(event: dict, requested_conference: str) -> dict:
    """Reduce the events.insert response to the fields Claude cites in
    user-facing output, plus a `conference_status` line explaining what
    actually attached (or didn't)."""
    conf_data = event.get("conferenceData", {}) or {}
    entry_points = conf_data.get("entryPoints", []) or []
    join_url = next((ep.get("uri") for ep in entry_points if ep.get("entryPointType") == "video"), None)
    solution_name = (conf_data.get("conferenceSolution") or {}).get("name")

    if requested_conference == "none":
        status = "no conference requested"
    elif join_url and solution_name:
        status = f"attached: {solution_name}"
    elif join_url:
        status = "attached (solution name not surfaced by API)"
    else:
        status = (
            f"requested {requested_conference!r} but no conference entry "
            f"points attached. For Meet, the most likely cause is that "
            f"Google Meet creation is disabled for this calendar; for Zoom, "
            f"check that conferenceData was actually sent in the request "
            f"(pick_zoom_url may have raised silently). Retry with "
            f"--conference meet to confirm the pipeline."
        )

    return {
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "join_url": join_url,
        "conference_solution": solution_name,
        "conference_status": status,
        "attendees": [a.get("email") for a in event.get("attendees", [])],
        "start": (event.get("start") or {}).get("dateTime"),
        "end": (event.get("end") or {}).get("dateTime"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="ISO 8601 with offset, e.g. 2026-05-18T13:30:00-07:00")
    p.add_argument("--end", required=True, help="ISO 8601 with offset")
    p.add_argument("--summary", required=True, help="Event title")
    p.add_argument("--attendees", default="", help="Comma-separated emails (empty = self only)")
    p.add_argument("--description", default="")
    p.add_argument("--conference", choices=sorted(VALID_CONFERENCE_TYPES), help="Override config.yaml default")
    p.add_argument("--zoom-url", help="Ad-hoc Zoom URL to attach; overrides personal/pool resolution")
    p.add_argument("--impersonate", help="Service-account DWD subject (rare)")
    p.add_argument("--config", type=Path, default=CONFIG_FILE)
    p.add_argument("--dry-run", action="store_true", help="Print the request body and exit without calling the API")
    args = p.parse_args()

    start = dt.datetime.fromisoformat(args.start).astimezone()
    end = dt.datetime.fromisoformat(args.end).astimezone()
    attendees = [e for e in (a.strip() for a in args.attendees.split(",")) if e]
    conference = args.conference or load_default_conference(args.config)

    zoom_url: str | None = None
    if conference in {"zoom", "zoom-pool"}:
        try:
            zoom_url = pick_zoom_url(
                conference,
                start,
                override=args.zoom_url,
                personal=load_zoom_personal_url(args.config),
                fallback=load_zoom_fallback_rooms(args.config),
            )
        except ValueError as e:
            sys.exit(str(e))

    body = build_event_body(
        start, end, args.summary, attendees,
        description=args.description, conference=conference, zoom_url=zoom_url,
    )

    if args.dry_run:
        print(json.dumps({"would_send": body, "conference": conference}, indent=2))
        return

    creds = auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=args.impersonate)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        event = svc.events().insert(
            calendarId="primary",
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all",  # email invites to attendees
        ).execute()
    except HttpError as e:
        sys.exit(f"events.insert failed: HTTP {e.resp.status}: {e}")

    json.dump(summarize_response(event, requested_conference=conference), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
