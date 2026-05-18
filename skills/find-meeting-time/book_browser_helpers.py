"""book_browser_helpers.py — pure helpers for the Playwright booking path.

The browser booking flow lives in SKILL.md as a Playwright MCP recipe Claude
executes step-by-step. This module factors out the deterministic pieces
(time/date formatting, URL/snapshot parsing, response shaping) so they're
unit-testable in isolation from the MCP driver.

Not a runnable script — import as a module. Mirrors `create_event.py`'s
`summarize_response()` output shape so downstream code can treat both
booking paths interchangeably.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from urllib.parse import parse_qs, urlparse


def format_time_picker(t: dt.time | dt.datetime) -> str:
    """Format a time for Calendar's time input. The full event editor's
    time field accepts strings like "1:30 PM" / "9:00 AM" / "12:00 AM".
    Calendar normalizes back to "h:mm AM/PM" regardless of input case,
    but we emit the canonical form to avoid spurious re-snapshots."""
    if isinstance(t, dt.datetime):
        t = t.time()
    hour_12 = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{hour_12}:{t.minute:02d} {suffix}"


def format_date_picker(d: dt.date | dt.datetime) -> str:
    """Format a date for Calendar's date input. The full editor accepts
    "May 22, 2026" / "Dec 1, 2026" — short month, no leading zero on day.
    `%-d` is the POSIX no-pad day specifier (works on macOS and Linux)."""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.strftime("%b %-d, %Y")


def extract_eid_from_url(url: str) -> str | None:
    """Pull the `eid` from a Calendar event URL. Returns the raw
    URL-safe base64 string (or None if not present). Calendar uses
    several link shapes:
      https://www.google.com/calendar/event?eid=<eid>
      https://calendar.google.com/calendar/event?eid=<eid>
      https://calendar.google.com/calendar/u/0/r/eventedit/<eid>
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if qs.get("eid"):
        return qs["eid"][0]
    parts = parsed.path.split("/")
    if "eventedit" in parts:
        idx = parts.index("eventedit")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1]
    return None


def decode_eid(eid: str) -> tuple[str, str] | None:
    """Decode an eid into (event_id, calendar_email). Calendar's eid is
    URL-safe base64 of '<event_id> <calendar_email>' (no padding emitted
    by Google, so we re-pad before decoding). Returns None when the
    decoded blob doesn't have the expected single-space-separated shape
    (e.g., an obfuscated/shareable link variant)."""
    padded = eid + "=" * (-len(eid) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = raw.split(" ", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


_ZOOM_URL_RE = re.compile(r"https?://[\w-]+\.zoom\.us/(?:j|my|w)/\S+")
_TRAILING_PUNCT = ".,;:)\"'>]"


def extract_zoom_url(text: str) -> str | None:
    """Find the first Zoom join URL in a text blob — typically the
    Playwright snapshot region around "Joining info". Matches `/j/<id>`
    (regular meeting), `/my/<handle>` (personal room), and `/w/<id>`
    (webinar) forms. Trims trailing punctuation that snapshots often
    inherit from surrounding whitespace handling."""
    m = _ZOOM_URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(_TRAILING_PUNCT)


def shape_response(
    *,
    event_id: str,
    html_link: str,
    zoom_url: str | None,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    attendees: list[str],
) -> dict:
    """Return a dict in the same shape as `create_event.summarize_response`
    so consumers (Claude's echo step, logging, future automation) can treat
    API-path and browser-path booking results interchangeably. The only
    field that differs is `conference_status`, which records *how* the
    Zoom URL was attached."""
    return {
        "event_id": event_id,
        "html_link": html_link,
        "join_url": zoom_url,
        "conference_solution": "Zoom Meeting" if zoom_url else None,
        "conference_status": (
            "attached via Workspace add-on (browser path)"
            if zoom_url else "no Zoom URL captured"
        ),
        "attendees": list(attendees),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "summary": summary,
    }
