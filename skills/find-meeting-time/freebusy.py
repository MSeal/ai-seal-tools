#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
# ]
# ///
"""freebusy.py — query Google Calendar free/busy for a set of attendees.

Outputs structured JSON the find-meeting-time skill can rank. Three auth paths,
tried in order:

  1. Service account with domain-wide delegation — preferred for cross-user
     queries. Save the SA JSON at ~/.config/ai-seal-tools/google_service_account.json
     and pass --impersonate <your-email> so the SA acts as you (with consent
     from the user being impersonated implied by DWD setup). Solves the
     quota-project requirement and avoids per-user consent. See SETUP.md for
     the IT-request payload.

  2. Application Default Credentials (gcloud) — runs against gcloud's trusted
     OAuth client. Run once:
         gcloud auth application-default login \\
             --scopes=https://www.googleapis.com/auth/cloud-platform,\\
                     https://www.googleapis.com/auth/calendar.freebusy
     Requires a quota project the user has serviceusage.services.use on.

  3. Cached InstalledAppFlow token — used when an OAuth Desktop client is
     provisioned at ~/.config/ai-seal-tools/google_oauth_client.json.
     Token cached at ~/.config/ai-seal-tools/google_token.json.

Usage:
  uv run freebusy.py \\
      --emails a@example.com,b@example.com \\
      --start 2026-05-13T09:00 \\
      --end   2026-05-16T17:00 \\
      --duration 60 \\
      [--impersonate mseal@confluent.io]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from google.auth import default as adc_default
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]
CONFIG_DIR = Path.home() / ".config" / "ai-seal-tools"
SERVICE_ACCOUNT = CONFIG_DIR / "google_service_account.json"
CLIENT_SECRETS = CONFIG_DIR / "google_oauth_client.json"
TOKEN_FILE = CONFIG_DIR / "google_token.json"


def get_credentials(impersonate: str | None = None):
    # Path 1: service account with optional domain-wide delegation (best)
    if SERVICE_ACCOUNT.exists():
        sa = ServiceAccountCredentials.from_service_account_file(
            str(SERVICE_ACCOUNT), scopes=SCOPES
        )
        if impersonate:
            sa = sa.with_subject(impersonate)
        return sa

    # Path 2: cached InstalledAppFlow token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds

    # Path 3: fresh InstalledAppFlow (provisioned OAuth client; opens browser)
    if CLIENT_SECRETS.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
        creds = flow.run_local_server(port=0)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())
        return creds

    # Path 4: ADC (last resort — typically blocked by quota-project requirement)
    adc_error: str | None = None
    try:
        creds, _ = adc_default(scopes=SCOPES)
        creds.refresh(Request())
        return creds
    except (DefaultCredentialsError, RefreshError) as e:
        adc_error = f"{type(e).__name__}: {e}"

    sys.exit(
        "No usable Google Calendar credentials found.\n"
        f"  Expected one of:\n"
        f"    {CLIENT_SECRETS}   (OAuth Desktop client — most common)\n"
        f"    {SERVICE_ACCOUNT}  (service account + DWD — for cross-user access)\n"
        f"  See skills/find-meeting-time/SETUP.md for step-by-step setup.\n"
        f"  ADC fallback attempt failed with: {adc_error}"
    )


def find_candidate_slots(
    busy_by_email: dict[str, list[tuple[dt.datetime, dt.datetime]]],
    start: dt.datetime,
    end: dt.datetime,
    duration: dt.timedelta,
    work_start_hour: int = 9,
    work_end_hour: int = 17,
    step: dt.timedelta = dt.timedelta(minutes=15),
) -> list[dict]:
    """Slide a duration-sized window through [start, end] within working hours.

    For each window, count how many attendees have an overlapping busy block.
    Returns slots sorted by conflict count ascending (fully free first).
    """
    slots: list[dict] = []
    cursor = start
    while cursor + duration <= end:
        slot_end = cursor + duration
        local = cursor.astimezone()
        local_end = slot_end.astimezone()
        in_window = (
            local.hour >= work_start_hour
            and (local_end.hour < work_end_hour or (local_end.hour == work_end_hour and local_end.minute == 0))
            and local.date() == local_end.date()
            and local.weekday() < 5
        )
        if in_window:
            conflicts = [
                email
                for email, blocks in busy_by_email.items()
                if any(b_start < slot_end and b_end > cursor for b_start, b_end in blocks)
            ]
            slots.append({
                "start": local.isoformat(),
                "end": local_end.isoformat(),
                "conflicts": conflicts,
            })
        cursor += step
    slots.sort(key=lambda s: len(s["conflicts"]))
    return slots


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--emails", required=True, help="Comma-separated attendee emails")
    p.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-05-13T09:00")
    p.add_argument("--end", required=True, help="ISO 8601 (exclusive)")
    p.add_argument("--duration", type=int, required=True, help="Slot duration in minutes")
    p.add_argument("--work-start", type=int, default=9, help="Working-hours start (local hour, 24h)")
    p.add_argument("--work-end", type=int, default=17, help="Working-hours end (local hour, 24h)")
    p.add_argument("--impersonate", help="User email for service-account DWD impersonation")
    args = p.parse_args()

    emails = [e.strip() for e in args.emails.split(",") if e.strip()]
    start = dt.datetime.fromisoformat(args.start).astimezone()
    end = dt.datetime.fromisoformat(args.end).astimezone()
    duration = dt.timedelta(minutes=args.duration)

    creds = get_credentials(impersonate=args.impersonate)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    result = (
        service.freebusy()
        .query(body={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": e} for e in emails],
        })
        .execute()
    )

    calendars = result.get("calendars", {})
    busy_by_email: dict[str, list[tuple[dt.datetime, dt.datetime]]] = {}
    errors_by_email: dict[str, list[dict]] = {}
    for email in emails:
        cal = calendars.get(email, {})
        if errs := cal.get("errors"):
            errors_by_email[email] = errs
        busy_by_email[email] = [
            (dt.datetime.fromisoformat(b["start"]), dt.datetime.fromisoformat(b["end"]))
            for b in cal.get("busy", [])
        ]

    slots = find_candidate_slots(busy_by_email, start, end, duration, args.work_start, args.work_end)

    output = {
        "attendees": emails,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "duration_minutes": args.duration,
        "working_hours": {"start": args.work_start, "end": args.work_end},
        "busy": {
            email: [{"start": s.isoformat(), "end": e.isoformat()} for s, e in blocks]
            for email, blocks in busy_by_email.items()
        },
        "errors": errors_by_email,
        "candidate_slots": slots,
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
