#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
#   "pyyaml>=6.0",
# ]
# ///
"""freebusy.py — query Google Calendar availability for a set of attendees,
classify conflicts, and emit ranked candidate slots with ask-context.

Outputs structured JSON the find-meeting-time skill consumes. Uses
events.list (not freebusy.query) so we get event summaries and attendees
where the calendar sharing permits — needed for movability classification.

Auth paths tried in order:
  1. Service account at ~/.config/ai-seal-tools/google_service_account.json
     (+ --impersonate <email> for DWD)
  2. Cached InstalledAppFlow token at ~/.config/ai-seal-tools/google_token.json
  3. Fresh OAuth Desktop client flow from
     ~/.config/ai-seal-tools/google_oauth_client.json
  4. ADC fallback (usually blocked by quota-project requirement)

See SETUP.md for credential setup. Scope is calendar.events.readonly — when
upgrading from an older calendar.freebusy-only token, delete
~/.config/ai-seal-tools/google_token.json and re-run to redo consent.

Usage:
  uv run freebusy.py \\
      --emails a@example.com,b@example.com \\
      --start 2026-05-14T09:00 \\
      --end   2026-05-22T17:00 \\
      --duration 60 \\
      [--impersonate mseal@confluent.io] \\
      [--top 5]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).resolve().parent
# Resolve sibling modules (auth.py, seniority.py) without packaging.
sys.path.insert(0, str(SCRIPT_DIR))
import auth  # noqa: E402
from seniority import load_seniority, tier_for  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]
CONFIG_DIR = auth.CONFIG_DIR
CREDENTIALS_DIR = auth.CREDENTIALS_DIR
SERVICE_ACCOUNT = auth.SERVICE_ACCOUNT
CLIENT_SECRETS = auth.CLIENT_SECRETS
TOKEN_FILE = CREDENTIALS_DIR / "google_token.json"
SKILL_CONFIG_DIR = CONFIG_DIR / "find-meeting-time"
CONFIG_FILE = SKILL_CONFIG_DIR / "config.yaml"
PREFERENCES_FILE = SKILL_CONFIG_DIR / "preferences.md"
OUTCOMES_FILE = SKILL_CONFIG_DIR / "outcomes.jsonl"
SENIORITY_FILE = SKILL_CONFIG_DIR / "seniority.yaml"

SCORE_WEIGHTS_FILE = SCRIPT_DIR / "score_weights.yaml"
SCORE_WEIGHTS_LOCAL_FILE = SCRIPT_DIR / "score_weights.local.yaml"


@dataclass(frozen=True)
class ScoreWeights:
    """Tunable penalties used by score_slot. Defaults come from
    score_weights.yaml; per-user / per-session overrides come from
    score_weights.local.yaml (gitignored) alongside it.

    Rule: every magnitude that score_slot adds or subtracts MUST be a field
    on this dataclass — no literal numbers in score_slot's body. When adding
    a new penalty/bonus, add the field here AND the matching entry in
    score_weights.yaml (with a comment explaining what it controls) AND a
    test that the value flows through. See project CLAUDE.md > Scoring
    magnitudes.
    """
    conflict_movability_multiplier: int = 5
    lunch_overlap: int = 10
    day_edge_early: int = 5
    day_edge_late: int = 5
    attendee_tz_outside_hours: int = 20
    # When a recurring conflict appears >= threshold times in the queried
    # window (i.e. it's frequent — weekly cadence in a 3-week window, daily
    # in a 1-week window), skipping one instance is low-cost. Add the bonus
    # back per qualifying conflict.
    recurring_skippable_threshold: int = 3
    recurring_skippable_bonus: int = 5
    # Learned movability adjustment from outcomes.jsonl. For each
    # (event-fingerprint, attendee) tuple, recorded past outcomes shift
    # the per-conflict score: "moved" outcomes add credit (easier to ask
    # again), "declined" outcomes subtract (don't push it). Clamped to
    # ±learned_max_adjustment so a single attendee's history can't
    # dominate the structural ranking.
    learned_moved_bonus_per: int = 2
    learned_declined_penalty_per: int = 3
    learned_max_adjustment: int = 8
    # Seniority penalty — applied per conflict when the conflicting event
    # has at least one attendee whose recorded tier (in seniority.yaml)
    # exceeds the threshold. Penalty scales by tiers above threshold.
    seniority_threshold: int = 2
    seniority_penalty_per_tier_above: int = 5
    seniority_max_penalty: int = 20
    # Penalty multiplier for conflicts on "optional" attendees' calendars.
    # 1.0 = treat like required (no discount); 0.0 = ignore optional
    # conflicts entirely. Default 0.3 means optional conflicts contribute
    # ~a third of the penalty of required ones — they still inform the
    # ranking but don't dominate it.
    optional_attendee_penalty_multiplier: float = 0.3
    # Lead-time penalty — discourages proposing slots people don't have
    # time to prep for. Within-hour penalty linearly interpolates from
    # the full magnitude (slot at now) down to the same-day magnitude
    # (slot at now+60min). Same-day-later slots get the flat same-day
    # penalty. Tomorrow+ slots get nothing.
    lead_time_within_hour_penalty: int = 40
    lead_time_same_day_penalty: int = 10

    @classmethod
    def load(
        cls,
        defaults_path: Path = SCORE_WEIGHTS_FILE,
        local_path: Path = SCORE_WEIGHTS_LOCAL_FILE,
    ) -> "ScoreWeights":
        """Load defaults then overlay local overrides. Unknown keys are
        ignored with a stderr warning so typos don't silently take effect."""
        field_types = {f.name: f.type for f in dataclasses.fields(cls)}
        merged: dict[str, int | float] = {}
        for path in (defaults_path, local_path):
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text()) or {}
            for k, v in data.items():
                if k not in field_types:
                    print(f"[freebusy.py] {path.name}: unknown weight key {k!r}, ignoring", file=sys.stderr)
                    continue
                # Cast YAML scalar to the dataclass field's declared type.
                caster = float if field_types[k] == "float" else int
                merged[k] = caster(v)
        return cls(**merged)

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DEFAULT_WORK_HOURS = "09:00-17:00"


def load_working_hours(path: Path = CONFIG_FILE) -> dict[str, tuple[dt.time, dt.time]]:
    """Read working hours from config.yaml. Returns dict[day_name → (start, end)].

    config.yaml shape (all optional):
        working_hours:
          default: "09:00-17:00"
          friday:  "09:00-15:00"      # per-day override

    Missing file or missing section → every day uses DEFAULT_WORK_HOURS.
    """
    raw_default = DEFAULT_WORK_HOURS
    per_day: dict[str, str] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        wh = (data.get("working_hours") or {})
        if isinstance(wh, str):
            raw_default = wh
        else:
            raw_default = wh.get("default", DEFAULT_WORK_HOURS)
            per_day = {k.lower(): v for k, v in wh.items() if k.lower() in DAY_NAMES}

    def parse(spec: str) -> tuple[dt.time, dt.time]:
        a, b = spec.split("-", 1)
        return dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())

    default_pair = parse(raw_default)
    return {day: parse(per_day[day]) if day in per_day else default_pair for day in DAY_NAMES}


def load_attendee_timezones(path: Path = CONFIG_FILE) -> dict[str, str]:
    """Read per-attendee timezones from config.yaml. Returns email (lowercased)
    → IANA tz name (e.g., "America/New_York"). Empty dict if missing.

    config.yaml shape (optional):
        attendee_timezones:
          alice@example.com: America/New_York
          bob@example.com:   Europe/Berlin

    Attendees without an entry are scored as if they share the system TZ
    (today's behavior). Add entries selectively for known distributed people.
    Travel / temporary overrides go in attendee_timezone_exceptions; see
    load_attendee_timezone_exceptions.
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    tz_map = data.get("attendee_timezones") or {}
    return {str(email).lower(): str(tz) for email, tz in tz_map.items() if tz}


def _to_date(v) -> dt.date:
    """Coerce YAML-loaded values to a date. PyYAML parses `2026-05-13`
    natively as a date, but if the user quoted it we get a string."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def load_attendee_timezone_exceptions(path: Path = CONFIG_FILE) -> list[dict]:
    """Read time-windowed TZ overrides from config.yaml. Each entry has
    {email (lowercased), tz, start: date, end: date, note: str | None}.

    config.yaml shape (optional):
        attendee_timezone_exceptions:
          - email: carol@example.com
            tz:    America/New_York
            start: 2026-05-13
            end:   2026-05-16
            note:  NYC travel

    `start` and `end` are inclusive. When a slot's date falls within an
    exception window, that TZ overrides the entry in attendee_timezones
    for the affected attendee. Malformed entries are skipped with a
    stderr note rather than aborting the run.
    """
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("attendee_timezone_exceptions") or []
    parsed: list[dict] = []
    for entry in raw:
        try:
            parsed.append({
                "email": str(entry["email"]).lower(),
                "tz": str(entry["tz"]),
                "start": _to_date(entry["start"]),
                "end": _to_date(entry["end"]),
                "note": (str(entry["note"]) if entry.get("note") else None),
            })
        except (KeyError, ValueError, TypeError) as e:
            print(f"[freebusy.py] skipping malformed tz exception {entry!r}: {e}", file=sys.stderr)
    return parsed


def event_fingerprint(event: dict) -> str:
    """Stable key for matching outcomes across runs.

    For recurring events the recurringEventId is highly stable across instances
    so we use it directly. For one-offs we hash the (summary, sorted attendee
    emails) tuple — fragile if the meeting gets renamed or attendees shuffle,
    but the best we can do without the API exposing a stable ID for one-offs.
    """
    if rec_id := event.get("recurringEventId"):
        return f"rec::{rec_id}"
    summary = event.get("summary") or ""
    attendees = sorted(
        a.get("email", "").lower() for a in event.get("attendees", []) if a.get("email")
    )
    return f"oneoff::{summary}::{','.join(attendees)}"


def load_outcomes(path: Path = OUTCOMES_FILE) -> dict[tuple[str, str], dict[str, int]]:
    """Read outcomes.jsonl and aggregate per (event_fingerprint, attendee).

    Returns dict keyed by (fingerprint, lowercased_email) → counter dict:
        {"moved": int, "declined": int, "scheduled": int, ...,
         "last_outcome": str, "last_ts": str}

    Malformed lines are skipped with a stderr note rather than failing the
    whole run. Empty file or missing path returns an empty dict.
    """
    out: dict[tuple[str, str], dict[str, int | str]] = {}
    if not path.exists():
        return out
    for line_num, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
            fingerprint = rec["event_fingerprint"]
            attendee = str(rec["attendee"]).lower()
            outcome = rec["outcome"]
            ts = rec.get("ts", "")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"[freebusy.py] outcomes.jsonl line {line_num} malformed, skipping: {e}", file=sys.stderr)
            continue
        key = (fingerprint, attendee)
        agg = out.setdefault(key, {})
        agg[outcome] = int(agg.get(outcome, 0)) + 1
        # Track the most-recent timestamp so Claude can see "last asked X ago"
        if ts >= str(agg.get("last_ts", "")):
            agg["last_ts"] = ts
            agg["last_outcome"] = outcome
    return out  # type: ignore[return-value]


def _learned_adjustment(
    agg: dict[str, int | str] | None, weights: ScoreWeights
) -> tuple[int, dict[str, int] | None]:
    """Compute the score delta + counts to surface in breakdown."""
    if not agg:
        return 0, None
    counts = {k: v for k, v in agg.items() if isinstance(v, int)}
    moved = counts.get("moved", 0) + counts.get("agreed", 0)
    declined = counts.get("declined", 0)
    delta = moved * weights.learned_moved_bonus_per - declined * weights.learned_declined_penalty_per
    delta = max(-weights.learned_max_adjustment, min(weights.learned_max_adjustment, delta))
    return delta, counts


def _effective_tz_for(
    email: str,
    on_date: dt.date,
    base: dict[str, str],
    exceptions: list[dict],
) -> tuple[str | None, str | None]:
    """Resolve (tz_name, note) for an attendee on a given date. Exceptions
    take priority over the base map; multiple overlapping exceptions resolve
    to the first match (declaration order)."""
    e = email.lower()
    for ex in exceptions:
        if ex["email"] == e and ex["start"] <= on_date <= ex["end"]:
            return ex["tz"], ex.get("note")
    return base.get(e), None


# ----------------------------------------------------------------------------
# Auth — delegates to auth.py so create_event.py (write scope) shares the
# same credential lifecycle without code duplication.
# ----------------------------------------------------------------------------

def get_credentials(impersonate: str | None = None):
    return auth.get_credentials(SCOPES, TOKEN_FILE, impersonate=impersonate)


# ----------------------------------------------------------------------------
# Event fetch
# ----------------------------------------------------------------------------

def fetch_events(service, email: str, start: dt.datetime, end: dt.datetime) -> tuple[list[dict], str | None]:
    """Fetch events on a calendar. Returns (events, error_msg).

    If the calendar is unreadable (403), returns ([], reason). All events,
    including ones whose details aren't visible to the caller (Workspace's
    "free/busy only" sharing), come back with whatever fields Google exposes
    — typically start/end always, summary only when sharing permits.
    """
    events: list[dict] = []
    page_token: str | None = None
    while True:
        try:
            result = service.events().list(
                calendarId=email,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
                maxResults=2500,
            ).execute()
        except HttpError as e:
            return [], f"HTTP {e.resp.status}: {e.error_details if hasattr(e, 'error_details') else e}"
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return events, None


def parse_event_time(event: dict, key: str) -> dt.datetime | None:
    """Parse start/end from an event. Handles dateTime, date (all-day), and missing."""
    t = event.get(key, {})
    if dt_str := t.get("dateTime"):
        return dt.datetime.fromisoformat(dt_str)
    if date_str := t.get("date"):
        # All-day events: treat as [00:00, 24:00) local on that date
        d = dt.date.fromisoformat(date_str)
        is_end = key == "end"
        return dt.datetime.combine(d, dt.time(0, 0)).astimezone()
    return None


# ----------------------------------------------------------------------------
# Movability classifier
# ----------------------------------------------------------------------------

# (regex pattern, category, movability 0-10). First match wins, so order
# matters: put strong-signal "don't move this" rules before broader patterns
# that might also match the same title (e.g. "OOO Travel" → ooo, not travel).
TITLE_RULES: list[tuple[re.Pattern[str], str, int]] = [
    # OOO / immovable — match first so "OOO Travel to NYC" categorizes as ooo
    # not travel. "DNS" / "DNB" are Confluent conventions for "Do Not Schedule".
    (re.compile(r"\b(ooo|out\s*of\s*office|pto|vacation|holiday|sick|appt|appointment|doctor|dentist|dns|dnb|do\s*not\s*(schedule|book))\b", re.I), "ooo", 0),
    # High-stakes internal — also strong "don't move" signal
    (re.compile(r"\b(interview|phone\s*screen|onsite|hiring|loop)\b", re.I), "interview", 1),
    (re.compile(r"\b(all[\s\-]*hands|town\s*hall|company\s*meeting)\b", re.I), "all_hands", 1),
    (re.compile(r"\b(exec|leadership|board|qbr)\b", re.I), "exec_sync", 2),
    # External / customer — harder to move than internal meetings
    (re.compile(r"\b(customer|client|external|prospect|vendor|partner)\b", re.I), "customer_meeting", 2),
    (re.compile(r"\b(demo|sales|onboarding)\b", re.I), "customer_meeting", 3),
    # Highly movable personal time blocks
    (re.compile(r"\b(focus|deep\s*work|heads?\s*down|dnd|no\s*meetings?)\b", re.I), "focus_block", 10),
    (re.compile(r"\b(hold|placeholder|tentative|optional|block)\b", re.I), "personal_hold", 9),
    (re.compile(r"\b(travel|commute|wfh|working\s*location)\b", re.I), "travel_block", 7),
    # Personal events
    (re.compile(r"\b(lunch|coffee|breakfast|dinner)\b", re.I), "meal", 6),
    (re.compile(r"\b(workout|gym|run|yoga)\b", re.I), "personal", 6),
    # 1:1s — recurring 1:1s are typically the easiest real meeting to shift.
    (re.compile(r"\b(1\s*[:\-/x]\s*1|1on1|one\s*on\s*one)\b", re.I), "one_on_one", 8),
    (re.compile(r"^\s*[\w.+-]+\s*[/<>&]\s*[\w.+-]+\s*$", re.I), "one_on_one", 8),  # "alice/bob" pattern
    # Recurring team meetings
    (re.compile(r"\b(standup|stand\-up|daily\s*sync|scrum)\b", re.I), "team_standup", 5),
    (re.compile(r"\b(team\s*sync|team\s*meeting|weekly|sync|sprint|retro|grooming|planning|review)\b", re.I), "team_meeting", 5),
]


def classify_event(event: dict, *, series_counts: dict[str, int] | None = None) -> dict:
    """Return classification dict. Optional `series_counts` maps
    recurringEventId → how many times that series appears in the queried
    window (per attendee). Used to surface `frequency_in_window` so Claude
    can write "appears 4 times in the next two weeks" vs "only once" in
    ask-messages."""
    summary = event.get("summary")
    visible = summary is not None
    event_type = event.get("eventType", "default")
    status = event.get("status", "confirmed")
    transparency = event.get("transparency", "opaque")
    attendees = event.get("attendees", [])
    attendee_emails = sorted({
        a.get("email", "").lower() for a in attendees if a.get("email")
    })
    recurring_event_id = event.get("recurringEventId")
    recurring = recurring_event_id is not None
    is_all_day = "date" in event.get("start", {})
    # How many times this series appears in the queried window (caller-supplied).
    # 1 by default for one-offs or when we don't have count info. A high number
    # tells you the cadence: weekly events surface 2-3 times in a typical
    # 2-week query, monthly only 1.
    frequency_in_window = 1
    if recurring_event_id and series_counts:
        frequency_in_window = series_counts.get(recurring_event_id, 1)

    # eventType signals take priority over title parsing
    if event_type == "outOfOffice":
        category, movability = "ooo", 0
    elif event_type == "focusTime":
        category, movability = "focus_block", 10
    elif event_type == "workingLocation":
        category, movability = "working_location", 10  # not really a conflict
    elif status == "tentative":
        category, movability = "tentative", 9
    elif not visible:
        # Workspace "free/busy only" sharing — we see the event but not the title
        category, movability = "opaque", 5  # neutral default; user can't auto-decide
    else:
        category, movability = "generic_meeting", 5  # default before title scan
        for pattern, cat, mov in TITLE_RULES:
            if pattern.search(summary):
                category, movability = cat, mov
                break

    # Tentative status weakens any classification by 1 step
    if status == "tentative" and category != "tentative":
        movability = min(10, movability + 1)

    # Larger meetings are harder to shift (coordination cost)
    if attendees and len(attendees) >= 6 and movability > 2:
        movability = max(2, movability - 2)

    # All-day events are usually OOO-like
    if is_all_day and category == "generic_meeting":
        category, movability = "all_day_block", 1

    return {
        "category": category,
        "movability": movability,
        "recurring": recurring,
        "recurring_event_id": recurring_event_id,
        "frequency_in_window": frequency_in_window,
        "fingerprint": event_fingerprint(event),
        "visible": visible,
        "summary": summary,
        "attendee_count": len(attendees),
        "attendee_emails": attendee_emails,
        "status": status,
        "transparency": transparency,
        "is_all_day": is_all_day,
    }


def event_blocks_time(event: dict, classification: dict) -> bool:
    """Should this event count as a conflict for slot-finding?"""
    if classification["transparency"] == "transparent":
        return False  # event explicitly marked as not blocking
    if classification["status"] == "cancelled":
        return False
    if classification["category"] == "working_location":
        return False  # just a location marker
    # Declined invites — user has said no, so we don't count them
    for a in event.get("attendees", []):
        if a.get("self") and a.get("responseStatus") == "declined":
            return False
    return True


# ----------------------------------------------------------------------------
# Slot finding + scoring
# ----------------------------------------------------------------------------

def build_busy_map(
    events_by_email: dict[str, list[dict]]
) -> dict[str, list[tuple[dt.datetime, dt.datetime, dict]]]:
    """For each attendee, list (start, end, classification) for blocking events.
    Counts recurring-series occurrences per attendee in a first pass so each
    event's classification can carry its `frequency_in_window`."""
    out: dict[str, list[tuple[dt.datetime, dt.datetime, dict]]] = {}
    for email, events in events_by_email.items():
        # First pass: how many times does each series appear in this window?
        series_counts: dict[str, int] = {}
        for ev in events:
            rec_id = ev.get("recurringEventId")
            if rec_id:
                series_counts[rec_id] = series_counts.get(rec_id, 0) + 1

        blocks: list[tuple[dt.datetime, dt.datetime, dict]] = []
        for ev in events:
            cls = classify_event(ev, series_counts=series_counts)
            if not event_blocks_time(ev, cls):
                continue
            start = parse_event_time(ev, "start")
            end = parse_event_time(ev, "end")
            if not start or not end:
                continue
            blocks.append((start, end, cls))
        out[email] = blocks
    return out


def _lead_time_penalty(
    slot_start: dt.datetime,
    now: dt.datetime,
    weights: ScoreWeights,
) -> tuple[int, str | None]:
    """Penalize slots that are too soon. Returns (penalty, label).

    Curve:
      [0, 60) min from now → linearly interpolate from within_hour down
                             to same_day (0 min = full within-hour penalty,
                             60 min = full same-day penalty)
      [60 min, same-day-end] → flat same_day
      next day onwards     → 0
    Slots in the past get the full within-hour penalty (defensive — the
    helper should already have filtered them, but treat them harshly if
    they slip through).
    """
    minutes_until = (slot_start - now).total_seconds() / 60
    if minutes_until <= 0:
        return weights.lead_time_within_hour_penalty, "in the past (clock drift)"
    if minutes_until < 60:
        ratio = minutes_until / 60
        penalty = int(round(
            weights.lead_time_within_hour_penalty * (1 - ratio)
            + weights.lead_time_same_day_penalty * ratio
        ))
        return penalty, f"~{int(minutes_until)} min from now"
    if slot_start.astimezone().date() == now.astimezone().date():
        return weights.lead_time_same_day_penalty, "same-day, short notice"
    return 0, None


def _outside_working_hours_local(
    local_start: dt.datetime,
    local_end: dt.datetime,
    work_start: dt.time,
    work_end: dt.time,
) -> bool:
    """True if the slot is outside the working window in the given local TZ.
    Handles weekends and midnight-spanning slots in that TZ."""
    if local_start.weekday() >= 5 or local_end.weekday() >= 5:
        return True
    if local_start.date() != local_end.date():
        return True
    return not (local_start.time() >= work_start and local_end.time() <= work_end)


def score_slot(
    cursor: dt.datetime,
    slot_end: dt.datetime,
    conflicts: list[dict],
    work_start: dt.time,
    work_end: dt.time,
    *,
    attendees: list[str] | None = None,
    required_attendees: set[str] | None = None,
    attendee_timezones: dict[str, str] | None = None,
    attendee_timezone_exceptions: list[dict] | None = None,
    outcomes_aggregated: dict[tuple[str, str], dict[str, int | str]] | None = None,
    seniority_map: dict[str, dict] | None = None,
    now: dt.datetime | None = None,
    weights: ScoreWeights | None = None,
) -> dict:
    """Return {score, breakdown} for a candidate slot.

    Structural penalties: conflicts (weighted by inverse movability), lunch
    overlap, day-edge slots, and (when `attendee_timezones` is supplied)
    "slot lands outside this attendee's working hours in their local TZ".
    Magnitudes come from `weights` (defaults match score_weights.yaml).
    Subjective overrides (day-of-week biases, defended blocks, etc.) come
    from preferences.md which Claude consumes in SKILL.md execution.
    """
    if weights is None:
        weights = ScoreWeights()

    breakdown: list[dict] = []
    score = 100

    for c in conflicts:
        is_optional = (
            required_attendees is not None
            and c["attendee"].lower() not in required_attendees
        )
        base_penalty = (10 - c["conflict"]["movability"]) * weights.conflict_movability_multiplier
        penalty = int(round(base_penalty * weights.optional_attendee_penalty_multiplier)) if is_optional else base_penalty
        score -= penalty
        label = f"conflict: {c['attendee']} ({c['conflict']['category']})"
        if is_optional:
            label += " [optional]"
        breakdown.append({"label": label, "delta": -penalty})

        # High-frequency recurring conflicts get a bonus: easier to skip one
        # instance when the series happens often.
        freq = c["conflict"].get("frequency_in_window", 1)
        if c["conflict"].get("recurring") and freq >= weights.recurring_skippable_threshold:
            bonus = weights.recurring_skippable_bonus
            score += bonus
            breakdown.append({
                "label": f"recurring & frequent: {c['attendee']} ({freq}× in window)",
                "delta": bonus,
            })

        # Learned adjustment from outcomes.jsonl history for this (event, attendee).
        if outcomes_aggregated and (fp := c["conflict"].get("fingerprint")):
            agg = outcomes_aggregated.get((fp, c["attendee"].lower()))
            delta, counts = _learned_adjustment(agg, weights)
            if delta != 0 and counts:
                score += delta
                counts_summary = ", ".join(f"{k}:{v}" for k, v in counts.items() if v > 0)
                breakdown.append({
                    "label": f"learned: {c['attendee']} / {c['conflict'].get('summary') or 'opaque'} ({counts_summary})",
                    "delta": delta,
                })

        # Seniority penalty — if the conflicting event has senior attendees,
        # moving it is harder regardless of who we're directly asking.
        if seniority_map:
            conflict_attendees = c["conflict"].get("conflict_attendees") or []
            ranked = [(e, tier_for(e, seniority_map)) for e in conflict_attendees]
            ranked = [(e, t) for e, t in ranked if t > 0]
            if ranked:
                top_email, max_tier = max(ranked, key=lambda x: x[1])
                if max_tier > weights.seniority_threshold:
                    penalty = (max_tier - weights.seniority_threshold) * weights.seniority_penalty_per_tier_above
                    penalty = min(penalty, weights.seniority_max_penalty)
                    score -= penalty
                    breakdown.append({
                        "label": f"senior attendee on conflict: {top_email} (tier {max_tier})",
                        "delta": -penalty,
                    })

    local = cursor.astimezone()
    local_end = slot_end.astimezone()
    date = local.date()

    # Lunch overlap (12:00-13:00 local) — universal heuristic; override via preferences.md
    lunch_start = dt.datetime.combine(date, dt.time(12, 0), tzinfo=local.tzinfo)
    lunch_end = dt.datetime.combine(date, dt.time(13, 0), tzinfo=local.tzinfo)
    if local < lunch_end and local_end > lunch_start:
        score -= weights.lunch_overlap
        breakdown.append({"label": "lunch overlap", "delta": -weights.lunch_overlap})

    # Day-edge penalties — first/last 30 min of working hours
    wh_start_dt = dt.datetime.combine(date, work_start, tzinfo=local.tzinfo)
    wh_end_dt = dt.datetime.combine(date, work_end, tzinfo=local.tzinfo)
    if local < wh_start_dt + dt.timedelta(minutes=30):
        score -= weights.day_edge_early
        breakdown.append({"label": "day-edge (early)", "delta": -weights.day_edge_early})
    if local_end > wh_end_dt - dt.timedelta(minutes=30):
        score -= weights.day_edge_late
        breakdown.append({"label": "day-edge (late)", "delta": -weights.day_edge_late})

    # Lead-time penalty — discourage proposing slots people don't have
    # time to prep for. Heavier within the hour, lighter same-day.
    if now is not None:
        lead_penalty, lead_label = _lead_time_penalty(cursor, now, weights)
        if lead_penalty > 0:
            score -= lead_penalty
            breakdown.append({
                "label": f"lead time: {lead_label}" if lead_label else "lead time",
                "delta": -lead_penalty,
            })

    # Cross-attendee TZ check — penalize slots outside an attendee's
    # working hours in their own local TZ. Only fires for attendees with
    # a configured base TZ (or an active exception window) in config.yaml;
    # others are scored as if they share the system TZ.
    if attendees and (attendee_timezones or attendee_timezone_exceptions):
        base = attendee_timezones or {}
        exceptions = attendee_timezone_exceptions or []
        slot_date = local.date()
        for email in attendees:
            tz_name, note = _effective_tz_for(email, slot_date, base, exceptions)
            if not tz_name:
                continue
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                continue
            a_start = cursor.astimezone(tz)
            a_end = slot_end.astimezone(tz)
            if _outside_working_hours_local(a_start, a_end, work_start, work_end):
                score -= weights.attendee_tz_outside_hours
                local_str = a_start.strftime("%a %-I:%M%p").lower()
                label = f"outside working hours for {email} ({local_str} {tz_name}"
                if note:
                    label += f", {note}"
                label += ")"
                breakdown.append({"label": label, "delta": -weights.attendee_tz_outside_hours})

    score = max(0, min(100, score))
    return {"score": score, "breakdown": breakdown}


def make_ask_context(
    attendee: str,
    conflict_block: tuple[dt.datetime, dt.datetime, dict],
    outcomes_aggregated: dict[tuple[str, str], dict[str, int | str]] | None = None,
) -> dict:
    """Structured per-conflict data the skill renders into ask-messages."""
    start, end, cls = conflict_block
    history: dict | None = None
    if outcomes_aggregated and (fp := cls.get("fingerprint")):
        if (agg := outcomes_aggregated.get((fp, attendee.lower()))):
            history = dict(agg)  # shallow copy so callers don't mutate
    return {
        "attendee": attendee,
        "conflict": {
            "visible": cls["visible"],
            "summary": cls["summary"],
            "category": cls["category"],
            "movability": cls["movability"],
            "recurring": cls["recurring"],
            "recurring_event_id": cls.get("recurring_event_id"),
            "frequency_in_window": cls.get("frequency_in_window", 1),
            "fingerprint": cls.get("fingerprint"),
            "outcome_history": history,
            "status": cls["status"],
            "is_all_day": cls["is_all_day"],
            "attendee_count": cls["attendee_count"],
            "conflict_attendees": cls.get("attendee_emails", []),
            "conflict_start": start.astimezone().isoformat(),
            "conflict_end": end.astimezone().isoformat(),
        },
    }


def find_candidate_slots(
    busy_by_email: dict[str, list[tuple[dt.datetime, dt.datetime, dict]]],
    start: dt.datetime,
    end: dt.datetime,
    duration: dt.timedelta,
    working_hours: dict[str, tuple[dt.time, dt.time]],
    step: dt.timedelta = dt.timedelta(minutes=15),
    *,
    attendees: list[str] | None = None,
    required_attendees: set[str] | None = None,
    attendee_timezones: dict[str, str] | None = None,
    attendee_timezone_exceptions: list[dict] | None = None,
    outcomes_aggregated: dict[tuple[str, str], dict[str, int | str]] | None = None,
    seniority_map: dict[str, dict] | None = None,
    now: dt.datetime | None = None,
    weights: ScoreWeights | None = None,
) -> list[dict]:
    """Slide a duration-sized window through [start, end] within each day's
    configured working hours. Weekends skipped."""
    slots: list[dict] = []
    cursor = start
    while cursor + duration <= end:
        slot_end = cursor + duration
        local = cursor.astimezone()
        local_end = slot_end.astimezone()
        if local.date() != local_end.date() or local.weekday() >= 5:
            cursor += step
            continue

        day_name = DAY_NAMES[local.weekday()]
        wh_start, wh_end = working_hours[day_name]
        wh_start_dt = dt.datetime.combine(local.date(), wh_start, tzinfo=local.tzinfo)
        wh_end_dt = dt.datetime.combine(local.date(), wh_end, tzinfo=local.tzinfo)
        if local < wh_start_dt or local_end > wh_end_dt:
            cursor += step
            continue

        conflicts: list[dict] = []
        for email, blocks in busy_by_email.items():
            for b_start, b_end, cls in blocks:
                if b_start < slot_end and b_end > cursor:
                    conflicts.append(make_ask_context(email, (b_start, b_end, cls), outcomes_aggregated))
                    break  # one conflict per attendee per slot is enough

        scoring = score_slot(
            cursor, slot_end, conflicts, wh_start, wh_end,
            attendees=attendees,
            required_attendees=required_attendees,
            attendee_timezones=attendee_timezones,
            attendee_timezone_exceptions=attendee_timezone_exceptions,
            outcomes_aggregated=outcomes_aggregated,
            seniority_map=seniority_map,
            now=now,
            weights=weights,
        )
        slots.append({
            "start": local.isoformat(),
            "end": local_end.isoformat(),
            "score": scoring["score"],
            "score_breakdown": scoring["breakdown"],
            "conflicts": conflicts,
        })
        cursor += step

    return slots


def conflict_signature(slot: dict) -> tuple:
    """Hashable fingerprint of a slot's conflicts (attendee + event identity).

    Two slots with the same conflict signature offer the same trade-off — there's
    no reason to surface both. Empty tuple means "all-free" and isn't deduped.
    """
    return tuple(sorted(
        (c["attendee"], c["conflict"].get("summary") or c["conflict"]["category"])
        for c in slot["conflicts"]
    ))


def dedup_and_rank(slots: list[dict], top_n: int) -> list[dict]:
    """Greedily pick the top N slots by score, with two dedup rules:
    1. No two slots may overlap in time.
    2. No two slots may share the same conflict signature (same attendees with
       the same blocking events) — that's the same negotiation twice.
    """
    selected: list[dict] = []
    seen_sigs: set[tuple] = set()
    for s in sorted(slots, key=lambda x: (-x["score"], x["start"])):
        if len(selected) >= top_n:
            break
        s_start = dt.datetime.fromisoformat(s["start"])
        s_end = dt.datetime.fromisoformat(s["end"])
        if any(
            dt.datetime.fromisoformat(t["start"]) < s_end
            and dt.datetime.fromisoformat(t["end"]) > s_start
            for t in selected
        ):
            continue
        sig = conflict_signature(s)
        if sig and sig in seen_sigs:
            continue  # same trade-off as an already-selected slot
        seen_sigs.add(sig)
        selected.append(s)
    return selected


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--emails", required=True, help="Comma-separated attendee emails (required attendees)")
    p.add_argument("--optional", default="", help="Comma-separated optional-attendee emails. Their calendars are still queried, but conflicts apply a reduced penalty (see optional_attendee_penalty_multiplier in score_weights.yaml).")
    p.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-05-14T09:00")
    p.add_argument("--end", required=True, help="ISO 8601 (exclusive)")
    p.add_argument("--duration", type=int, required=True, help="Slot duration in minutes")
    p.add_argument("--work-start", type=str, help="Working-hours start HH:MM (overrides config)")
    p.add_argument("--work-end", type=str, help="Working-hours end HH:MM (overrides config)")
    p.add_argument("--impersonate", help="User email for service-account DWD impersonation")
    p.add_argument("--top", type=int, default=5, help="Number of ranked slots to return (default 5)")
    p.add_argument("--config", type=Path, default=CONFIG_FILE, help=f"Path to config.yaml (default {CONFIG_FILE})")
    args = p.parse_args()

    required_list = [e.strip() for e in args.emails.split(",") if e.strip()]
    optional_list = [e.strip() for e in args.optional.split(",") if e.strip()]
    emails = required_list + [e for e in optional_list if e not in required_list]
    required_attendees = {e.lower() for e in required_list}
    start = dt.datetime.fromisoformat(args.start).astimezone()
    end = dt.datetime.fromisoformat(args.end).astimezone()
    duration = dt.timedelta(minutes=args.duration)

    working_hours = load_working_hours(args.config)
    if args.work_start or args.work_end:
        # CLI flags override every day's window for this run
        for day in DAY_NAMES:
            ws, we = working_hours[day]
            if args.work_start:
                ws = dt.time.fromisoformat(args.work_start)
            if args.work_end:
                we = dt.time.fromisoformat(args.work_end)
            working_hours[day] = (ws, we)

    creds = get_credentials(impersonate=args.impersonate)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    events_by_email: dict[str, list[dict]] = {}
    errors_by_email: dict[str, str] = {}
    for email in emails:
        events, err = fetch_events(service, email, start, end)
        if err:
            errors_by_email[email] = err
        events_by_email[email] = events

    attendee_timezones = load_attendee_timezones(args.config)
    attendee_timezone_exceptions = load_attendee_timezone_exceptions(args.config)
    outcomes_aggregated = load_outcomes()
    seniority_map = load_seniority(SENIORITY_FILE)
    weights = ScoreWeights.load()
    now = dt.datetime.now().astimezone()
    busy_by_email = build_busy_map(events_by_email)
    all_slots = find_candidate_slots(
        busy_by_email, start, end, duration, working_hours,
        attendees=emails,
        required_attendees=required_attendees,
        attendee_timezones=attendee_timezones,
        attendee_timezone_exceptions=attendee_timezone_exceptions,
        outcomes_aggregated=outcomes_aggregated,
        seniority_map=seniority_map,
        now=now,
        weights=weights,
    )
    top_slots = dedup_and_rank(all_slots, args.top)

    output = {
        "attendees": emails,
        "required_attendees": sorted(required_attendees),
        "optional_attendees": sorted(e.lower() for e in optional_list if e.lower() not in required_attendees),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "duration_minutes": args.duration,
        "config_path": str(args.config) if args.config.exists() else f"{args.config} (using defaults)",
        "preferences_path": str(PREFERENCES_FILE) if PREFERENCES_FILE.exists() else None,
        "outcomes_path": str(OUTCOMES_FILE) if OUTCOMES_FILE.exists() else None,
        "outcomes_loaded": sum(sum(v for v in agg.values() if isinstance(v, int)) for agg in outcomes_aggregated.values()),
        "seniority_path": str(SENIORITY_FILE) if SENIORITY_FILE.exists() else None,
        "seniority_entries_loaded": len(seniority_map),
        "now": now.isoformat(timespec="seconds"),
        "score_weights": dataclasses.asdict(weights),
        "working_hours": {d: f"{ws.isoformat(timespec='minutes')}-{we.isoformat(timespec='minutes')}" for d, (ws, we) in working_hours.items()},
        "attendee_timezones": attendee_timezones,
        "attendee_timezone_exceptions": [
            {**ex, "start": ex["start"].isoformat(), "end": ex["end"].isoformat()}
            for ex in attendee_timezone_exceptions
        ],
        "errors": errors_by_email,
        "ranked_slots": top_slots,
        "total_slots_considered": len(all_slots),
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
