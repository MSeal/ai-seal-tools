"""Tests for move_event.py — pure body builder + a patch boundary mock
(same pattern as test_create_event.py / test_sheets_writer.py)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import move_event as me


SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "move_event.py"


def _dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# build_patch_body — pure
# ---------------------------------------------------------------------------

def test_build_patch_body_only_carries_start_and_end():
    """The whole point of the script is to shift time without overwriting
    anything else. The body must contain *only* start/end — passing
    attendees here would replace the server-side list and drop RSVPs."""
    body = me.build_patch_body(
        _dt("2026-05-27T13:00:00-07:00"),
        _dt("2026-05-27T13:30:00-07:00"),
    )
    assert body == {
        "start": {"dateTime": "2026-05-27T13:00:00-07:00"},
        "end":   {"dateTime": "2026-05-27T13:30:00-07:00"},
    }
    assert set(body.keys()) == {"start", "end"}


# ---------------------------------------------------------------------------
# patch_event — calendar API boundary mock
# ---------------------------------------------------------------------------

def test_patch_event_calls_events_patch_with_send_updates_all_by_default():
    """Sanity check: the wrapper passes through sendUpdates correctly so
    attendees actually get the reschedule notice."""
    svc = mock.MagicMock()
    svc.events.return_value.patch.return_value.execute.return_value = {"id": "abc", "summary": "X"}

    result = me.patch_event(
        svc,
        "abc",
        {"start": {"dateTime": "2026-05-27T13:00:00-07:00"},
         "end":   {"dateTime": "2026-05-27T13:30:00-07:00"}},
    )

    call = svc.events.return_value.patch.call_args
    assert call.kwargs["calendarId"] == "primary"
    assert call.kwargs["eventId"] == "abc"
    assert call.kwargs["sendUpdates"] == "all"
    assert call.kwargs["body"]["start"] == {"dateTime": "2026-05-27T13:00:00-07:00"}
    assert result == {"id": "abc", "summary": "X"}


def test_patch_event_honors_explicit_send_updates_none():
    """Calling with send_updates='none' suppresses attendee notifications —
    used when the user wants to fix up a self-only hold quietly."""
    svc = mock.MagicMock()
    svc.events.return_value.patch.return_value.execute.return_value = {"id": "abc"}

    me.patch_event(svc, "abc", {"start": {}, "end": {}}, send_updates="none")
    call = svc.events.return_value.patch.call_args
    assert call.kwargs["sendUpdates"] == "none"


def test_patch_event_honors_non_primary_calendar():
    """Some events live on a shared/secondary calendar — caller can route
    the patch there explicitly."""
    svc = mock.MagicMock()
    svc.events.return_value.patch.return_value.execute.return_value = {"id": "x"}

    me.patch_event(svc, "x", {}, calendar_id="team-cal@group.calendar.google.com")
    call = svc.events.return_value.patch.call_args
    assert call.kwargs["calendarId"] == "team-cal@group.calendar.google.com"


# ---------------------------------------------------------------------------
# CLI smoke — --help and --dry-run never touch the network
# ---------------------------------------------------------------------------

def test_cli_help_exits_zero():
    """A first-line guarantee that the script imports and argparse builds.
    Catches missing deps, syntax errors, mis-named imports — cheap insurance."""
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "move_event.py" in r.stdout


def test_cli_dry_run_emits_patch_body_without_auth():
    """Dry-run path: no creds touched, prints the body the API would receive.
    Run via `uv run --script` so the script's own dep header is honored."""
    r = subprocess.run(
        [
            "uv", "run", "--script", str(SCRIPT),
            "evt_abc",
            "--start", "2026-05-27T13:00:00-07:00",
            "--end",   "2026-05-27T13:30:00-07:00",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    # Skip uv's "Installed ..." stderr noise; just parse stdout JSON.
    payload = json.loads(r.stdout)
    assert payload["would_patch"]["eventId"] == "evt_abc"
    assert payload["would_patch"]["sendUpdates"] == "all"
    assert payload["would_patch"]["body"]["start"]["dateTime"].startswith("2026-05-27T13:00:00")


def test_cli_rejects_end_before_start():
    """Caller sanity check — a swapped or zero-duration slot should fail
    early rather than silently produce a degenerate patch."""
    r = subprocess.run(
        [
            "uv", "run", "--script", str(SCRIPT),
            "evt_abc",
            "--start", "2026-05-27T13:30:00-07:00",
            "--end",   "2026-05-27T13:00:00-07:00",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "must be after" in r.stderr
