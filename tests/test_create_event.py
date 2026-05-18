"""Tests for create_event.py — pure helpers + a CLI smoke test that
mocks the Google Calendar service at the events().insert() boundary
(same pattern as test_sheets_writer.py)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import create_event as ce


SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "create_event.py"


def _dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# build_event_body — pure
# ---------------------------------------------------------------------------

def test_build_body_minimal_required_fields():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="Quick chat",
        attendees=["alice@x", "bob@x"],
        conference="none",
    )
    assert body["summary"] == "Quick chat"
    assert body["start"] == {"dateTime": "2026-05-18T13:30:00-07:00"}
    assert body["end"] == {"dateTime": "2026-05-18T14:00:00-07:00"}
    assert body["attendees"] == [{"email": "alice@x"}, {"email": "bob@x"}]


def test_build_body_lowercases_attendee_emails_and_strips():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["  Alice@X  ", "BOB@X"],
        conference="none",
    )
    assert body["attendees"] == [{"email": "alice@x"}, {"email": "bob@x"}]


def test_build_body_skips_empty_attendee_entries():
    """Commas in the user's CSV input may leave empty entries; don't
    insert {'email': ''} placeholders."""
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["", "alice@x", "   ", "bob@x"],
        conference="none",
    )
    assert body["attendees"] == [{"email": "alice@x"}, {"email": "bob@x"}]


def test_build_body_zoom_uses_hand_crafted_conference_data():
    """Zoom path attaches an existing URL via conferenceData (no
    createRequest, since Google's API only mints Meet that way)."""
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        conference="zoom",
        zoom_url="https://confluent.zoom.us/my/mseal",
    )
    conf = body["conferenceData"]
    assert "createRequest" not in conf  # not how we build Zoom anymore
    assert conf["conferenceSolution"]["key"]["type"] == "addOn"
    assert conf["conferenceSolution"]["name"] == "Zoom Meeting"
    assert conf["entryPoints"][0]["uri"] == "https://confluent.zoom.us/my/mseal"
    assert conf["entryPoints"][0]["entryPointType"] == "video"


def test_build_body_zoom_requires_zoom_url():
    """Selecting zoom without supplying a URL is a logic error in the caller
    (main() should resolve via pick_zoom_url first)."""
    import pytest
    with pytest.raises(ValueError, match="zoom_url must be supplied"):
        ce.build_event_body(
            _dt("2026-05-18T13:30:00-07:00"),
            _dt("2026-05-18T14:00:00-07:00"),
            summary="x",
            attendees=["a@x"],
            conference="zoom",
        )


def test_build_body_zoom_pool_uses_hand_crafted_too():
    """zoom-pool uses the same hand-crafted shape as zoom, just with a
    different URL source (selected by pick_zoom_url upstream)."""
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        conference="zoom-pool",
        zoom_url="https://confluent.zoom.us/j/9999?pwd=x",
    )
    assert body["conferenceData"]["entryPoints"][0]["uri"] == "https://confluent.zoom.us/j/9999?pwd=x"


def test_build_body_meet_uses_hangouts_solution_key():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        conference="meet",
    )
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"


def test_build_body_none_omits_conference_data():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        conference="none",
    )
    assert "conferenceData" not in body


def test_build_body_includes_description_when_provided():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        description="Sync on AI tooling",
        conference="none",
    )
    assert body["description"] == "Sync on AI tooling"


def test_build_body_omits_description_when_empty():
    body = ce.build_event_body(
        _dt("2026-05-18T13:30:00-07:00"),
        _dt("2026-05-18T14:00:00-07:00"),
        summary="x",
        attendees=["a@x"],
        description="",
        conference="none",
    )
    assert "description" not in body


# ---------------------------------------------------------------------------
# summarize_response — pure
# ---------------------------------------------------------------------------

def test_summarize_response_extracts_zoom_join_url():
    """When the add-on attaches a video entry point, surface it as join_url."""
    event = {
        "id": "abc123",
        "htmlLink": "https://calendar.google.com/event?eid=abc123",
        "attendees": [{"email": "alice@x"}, {"email": "bob@x"}],
        "start": {"dateTime": "2026-05-18T13:30:00-07:00"},
        "end": {"dateTime": "2026-05-18T14:00:00-07:00"},
        "conferenceData": {
            "conferenceSolution": {"name": "Zoom Meeting"},
            "entryPoints": [{"entryPointType": "video", "uri": "https://confluent.zoom.us/j/12345"}],
        },
    }
    out = ce.summarize_response(event, requested_conference="zoom")
    assert out["event_id"] == "abc123"
    assert out["join_url"] == "https://confluent.zoom.us/j/12345"
    assert out["conference_solution"] == "Zoom Meeting"
    assert out["conference_status"] == "attached: Zoom Meeting"


def test_summarize_response_flags_addon_dispatch_failure():
    """The Zoom add-on may not dispatch — event still gets created but
    conferenceData has no entryPoints. The status field surfaces this
    so Claude can suggest the meet fallback."""
    event = {
        "id": "abc123",
        "htmlLink": "https://example",
        "conferenceData": {},
    }
    out = ce.summarize_response(event, requested_conference="zoom")
    assert out["join_url"] is None
    assert "no conference entry points" in out["conference_status"]
    assert "meet" in out["conference_status"]


def test_summarize_response_none_request_says_no_conference():
    event = {"id": "abc", "htmlLink": "https://example"}
    out = ce.summarize_response(event, requested_conference="none")
    assert out["conference_status"] == "no conference requested"


def test_summarize_response_extracts_meet_link():
    event = {
        "id": "abc",
        "htmlLink": "https://example",
        "conferenceData": {
            "conferenceSolution": {"name": "Google Meet"},
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                {"entryPointType": "more", "uri": "..."},
            ],
        },
    }
    out = ce.summarize_response(event, requested_conference="meet")
    assert out["join_url"] == "https://meet.google.com/abc-defg-hij"
    assert "Google Meet" in out["conference_status"]


# ---------------------------------------------------------------------------
# load_default_conference — config loader
# ---------------------------------------------------------------------------

def test_load_default_conference_missing_file_defaults_to_zoom(tmp_path):
    assert ce.load_default_conference(tmp_path / "no.yaml") == "zoom"


def test_load_default_conference_reads_each_valid_value(tmp_path):
    for value in ["zoom", "meet", "none"]:
        path = tmp_path / f"{value}.yaml"
        path.write_text(f"default_conference: {value}\n")
        assert ce.load_default_conference(path) == value


def test_load_default_conference_case_insensitive(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("default_conference: ZOOM\n")
    assert ce.load_default_conference(path) == "zoom"


def test_load_default_conference_unknown_value_falls_back_to_zoom(tmp_path, capsys):
    path = tmp_path / "x.yaml"
    path.write_text("default_conference: webex\n")
    assert ce.load_default_conference(path) == "zoom"
    assert "unknown" in capsys.readouterr().err.lower()


def test_load_default_conference_accepts_zoom_pool(tmp_path):
    """zoom-pool is a valid setting now (rotates through fallback rooms)."""
    path = tmp_path / "x.yaml"
    path.write_text("default_conference: zoom-pool\n")
    assert ce.load_default_conference(path) == "zoom-pool"


# ---------------------------------------------------------------------------
# load_zoom_personal_url / load_zoom_fallback_rooms
# ---------------------------------------------------------------------------

def test_load_zoom_personal_url_missing_file_none(tmp_path):
    assert ce.load_zoom_personal_url(tmp_path / "no.yaml") is None


def test_load_zoom_personal_url_returns_value(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text('zoom_personal_meeting_url: "https://confluent.zoom.us/my/mseal"\n')
    assert ce.load_zoom_personal_url(path) == "https://confluent.zoom.us/my/mseal"


def test_load_zoom_personal_url_empty_string_returns_none(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text('zoom_personal_meeting_url: ""\n')
    assert ce.load_zoom_personal_url(path) is None


def test_load_zoom_fallback_rooms_missing_file_empty(tmp_path):
    assert ce.load_zoom_fallback_rooms(tmp_path / "no.yaml") == []


def test_load_zoom_fallback_rooms_parses_list(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text(
        "zoom_fallback_rooms:\n"
        "  - https://confluent.zoom.us/j/111\n"
        "  - https://confluent.zoom.us/j/222\n"
    )
    assert ce.load_zoom_fallback_rooms(path) == [
        "https://confluent.zoom.us/j/111",
        "https://confluent.zoom.us/j/222",
    ]


def test_load_zoom_fallback_rooms_rejects_non_list(tmp_path):
    """Sanity: a non-list value returns empty, not raises."""
    path = tmp_path / "x.yaml"
    path.write_text('zoom_fallback_rooms: "not a list"\n')
    assert ce.load_zoom_fallback_rooms(path) == []


# ---------------------------------------------------------------------------
# pick_zoom_url — resolution priority
# ---------------------------------------------------------------------------

def test_pick_zoom_url_explicit_override_wins():
    """--zoom-url takes precedence over everything else."""
    url = ce.pick_zoom_url(
        "zoom", _dt("2026-05-18T13:00:00-07:00"),
        override="https://override/x",
        personal="https://personal/x",
        fallback=["https://pool/1", "https://pool/2"],
    )
    assert url == "https://override/x"


def test_pick_zoom_url_zoom_uses_personal():
    url = ce.pick_zoom_url(
        "zoom", _dt("2026-05-18T13:00:00-07:00"),
        override=None,
        personal="https://personal/x",
        fallback=[],
    )
    assert url == "https://personal/x"


def test_pick_zoom_url_zoom_no_personal_raises():
    import pytest
    with pytest.raises(ValueError, match="zoom_personal_meeting_url is not set"):
        ce.pick_zoom_url(
            "zoom", _dt("2026-05-18T13:00:00-07:00"),
            override=None, personal=None, fallback=[],
        )


def test_pick_zoom_url_pool_rotates_deterministically():
    """Pool selection should be deterministic per start time so re-running
    the same slot picks the same room (avoids surprises when retrying)."""
    fallback = ["https://pool/1", "https://pool/2", "https://pool/3"]
    start = _dt("2026-05-18T13:00:00-07:00")
    url1 = ce.pick_zoom_url("zoom-pool", start, override=None, personal=None, fallback=fallback)
    url2 = ce.pick_zoom_url("zoom-pool", start, override=None, personal=None, fallback=fallback)
    assert url1 == url2  # deterministic
    assert url1 in fallback


def test_pick_zoom_url_pool_varies_by_start():
    """Different start times generally land on different fallback rooms.
    Not strictly guaranteed by the hash, but with 3 rooms and many starts
    we should see at least two distinct picks across a day."""
    fallback = ["https://pool/1", "https://pool/2", "https://pool/3"]
    starts = [_dt(f"2026-05-18T{h:02d}:00:00-07:00") for h in range(9, 17)]
    picks = {
        ce.pick_zoom_url("zoom-pool", s, override=None, personal=None, fallback=fallback)
        for s in starts
    }
    assert len(picks) >= 2  # at least some variety across the day


def test_pick_zoom_url_pool_no_fallback_raises():
    import pytest
    with pytest.raises(ValueError, match="zoom_fallback_rooms is empty"):
        ce.pick_zoom_url(
            "zoom-pool", _dt("2026-05-18T13:00:00-07:00"),
            override=None, personal="https://personal/x", fallback=[],
        )


# ---------------------------------------------------------------------------
# CLI smoke (subprocess with --dry-run)
# ---------------------------------------------------------------------------

def test_cli_dry_run_prints_body_without_auth(tmp_path):
    """--dry-run avoids the auth path so this test runs offline. Confirms
    the CLI parses flags correctly, threads the zoom_url override through
    pick_zoom_url, and emits the hand-crafted conferenceData shape."""
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--start", "2026-05-18T13:30:00-07:00",
            "--end",   "2026-05-18T14:00:00-07:00",
            "--summary", "Quick chat",
            "--attendees", "alice@x,bob@x",
            "--conference", "zoom",
            "--zoom-url", "https://confluent.zoom.us/my/mseal",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["conference"] == "zoom"
    assert out["would_send"]["summary"] == "Quick chat"
    assert out["would_send"]["attendees"] == [{"email": "alice@x"}, {"email": "bob@x"}]
    conf = out["would_send"]["conferenceData"]
    assert "createRequest" not in conf
    assert conf["conferenceSolution"]["name"] == "Zoom Meeting"
    assert conf["entryPoints"][0]["uri"] == "https://confluent.zoom.us/my/mseal"


def test_cli_help_works():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "conference" in result.stdout.lower()
    assert "zoom" in result.stdout.lower()
