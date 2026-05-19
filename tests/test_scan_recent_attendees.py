"""Tests for scan_recent_attendees — the helper that lists recent
Calendar events, filters to small meetings, and merges attendee names
into people.yaml. Pure-function coverage of the filter logic and the
merge math; mocked-service coverage of the end-to-end scan."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest import mock

import yaml

import scan_recent_attendees as sra


# ---------------------------------------------------------------------------
# extract_attendees_from_event
# ---------------------------------------------------------------------------


def _event(
    *,
    attendees: list[dict] | None = None,
    start: dict | None = None,
    status: str = "confirmed",
    event_type: str | None = None,
) -> dict:
    e: dict = {"status": status}
    if attendees is not None:
        e["attendees"] = attendees
    if start is not None:
        e["start"] = start
    if event_type:
        e["eventType"] = event_type
    return e


def test_extract_returns_email_name_pairs_for_small_meeting():
    pairs = sra.extract_attendees_from_event(
        _event(
            start={"dateTime": "2026-05-15T10:00:00-07:00"},
            attendees=[
                {"email": "Alice@Example.com", "displayName": "Alice Example"},
                {"email": "bob@example.com", "displayName": "Bob Example"},
            ],
        ),
        max_attendees=5,
    )
    # Emails get lowercased; display names preserved.
    assert pairs == [
        ("alice@example.com", "Alice Example"),
        ("bob@example.com", "Bob Example"),
    ]


def test_extract_skips_large_meetings():
    """Above max_attendees the event is excluded entirely — large
    meetings are mostly noise for name-cache purposes."""
    attendees = [{"email": f"a{i}@example.com", "displayName": f"A{i}"} for i in range(7)]
    pairs = sra.extract_attendees_from_event(
        _event(start={"dateTime": "2026-05-15T10:00:00-07:00"}, attendees=attendees),
        max_attendees=5,
    )
    assert pairs == []


def test_extract_drops_resource_attendees():
    """Conference rooms come through events.list as attendees with
    resource=True; they shouldn't end up in the people cache."""
    pairs = sra.extract_attendees_from_event(
        _event(
            start={"dateTime": "2026-05-15T10:00:00-07:00"},
            attendees=[
                {"email": "alice@example.com", "displayName": "Alice Example"},
                {"email": "sf-room-1@resource.calendar.google.com", "resource": True},
            ],
        ),
        max_attendees=5,
    )
    assert pairs == [("alice@example.com", "Alice Example")]


def test_extract_skips_all_day_events():
    """PTO/holiday blocks have date (not dateTime) — they'd skew the
    cache with whole teams that just happen to be in the same all-day
    event."""
    pairs = sra.extract_attendees_from_event(
        _event(start={"date": "2026-05-15"}, attendees=[{"email": "alice@example.com"}]),
        max_attendees=5,
    )
    assert pairs == []


def test_extract_skips_cancelled_events():
    pairs = sra.extract_attendees_from_event(
        _event(
            status="cancelled",
            start={"dateTime": "2026-05-15T10:00:00-07:00"},
            attendees=[{"email": "alice@example.com"}],
        ),
        max_attendees=5,
    )
    assert pairs == []


def test_extract_skips_focus_and_ooo_event_types():
    """eventType=focusTime/outOfOffice/workingLocation are bookkeeping
    blocks, not real meetings."""
    for et in ("focusTime", "outOfOffice", "workingLocation"):
        pairs = sra.extract_attendees_from_event(
            _event(
                event_type=et,
                start={"dateTime": "2026-05-15T10:00:00-07:00"},
                attendees=[{"email": "alice@example.com"}],
            ),
            max_attendees=5,
        )
        assert pairs == [], f"event_type={et} should have been skipped"


def test_extract_handles_attendee_with_no_display_name():
    """Calendar doesn't always populate displayName (depends on
    directory sharing). Record the email anyway; a later Glean lookup
    can fill the name."""
    pairs = sra.extract_attendees_from_event(
        _event(
            start={"dateTime": "2026-05-15T10:00:00-07:00"},
            attendees=[{"email": "alice@example.com"}],
        ),
        max_attendees=5,
    )
    assert pairs == [("alice@example.com", None)]


def test_extract_skips_attendee_with_no_email():
    """A pathological attendee with no email field at all is dropped
    (rather than caching email='') — the cache is keyed by email."""
    pairs = sra.extract_attendees_from_event(
        _event(
            start={"dateTime": "2026-05-15T10:00:00-07:00"},
            attendees=[
                {"email": "alice@example.com", "displayName": "Alice"},
                {"displayName": "Nameless"},  # no email
            ],
        ),
        max_attendees=5,
    )
    assert pairs == [("alice@example.com", "Alice")]


# ---------------------------------------------------------------------------
# merge_attendees
# ---------------------------------------------------------------------------


def test_merge_adds_new_email_with_full_metadata():
    existing = {"people": {}}
    added, updated = sra.merge_attendees(
        existing,
        [("alice@example.com", "Alice Example")],
        event_date="2026-05-15",
        requester_email="you@example.com",
    )
    assert added == 1
    assert updated == 0
    assert existing["people"]["alice@example.com"] == {
        "name": "Alice Example",
        "last_seen": "2026-05-15",
        "sources": ["calendar-scan"],
    }


def test_merge_skips_requester_self():
    existing = {"people": {}}
    added, updated = sra.merge_attendees(
        existing,
        [("you@example.com", "Example User"), ("alice@example.com", "Alice")],
        event_date="2026-05-15",
        requester_email="you@example.com",
    )
    assert added == 1
    assert "you@example.com" not in existing["people"]


def test_merge_updates_last_seen_to_more_recent_event():
    """When the same person shows up across multiple events, last_seen
    advances to the most recent observation."""
    existing = {
        "people": {
            "alice@example.com": {
                "name": "Alice Example",
                "last_seen": "2026-05-10",
                "sources": ["calendar-scan"],
            }
        }
    }
    sra.merge_attendees(
        existing,
        [("alice@example.com", "Alice Example")],
        event_date="2026-05-15",
        requester_email=None,
    )
    assert existing["people"]["alice@example.com"]["last_seen"] == "2026-05-15"


def test_merge_does_not_regress_last_seen_for_older_event():
    """If we scan back further on a later run, the more-recent date
    already in the cache wins."""
    existing = {
        "people": {
            "alice@example.com": {
                "name": "Alice Example",
                "last_seen": "2026-05-15",
                "sources": ["calendar-scan"],
            }
        }
    }
    sra.merge_attendees(
        existing,
        [("alice@example.com", "Alice Example")],
        event_date="2026-05-10",  # older
        requester_email=None,
    )
    assert existing["people"]["alice@example.com"]["last_seen"] == "2026-05-15"


def test_merge_fills_missing_name_on_later_observation():
    """An entry recorded earlier without a displayName gets the name
    backfilled on a later event that did include it."""
    existing = {
        "people": {
            "alice@example.com": {
                "name": None,
                "last_seen": "2026-05-10",
                "sources": ["calendar-scan"],
            }
        }
    }
    added, updated = sra.merge_attendees(
        existing,
        [("alice@example.com", "Alice Example")],
        event_date="2026-05-15",
        requester_email=None,
    )
    assert added == 0
    assert updated == 1
    assert existing["people"]["alice@example.com"]["name"] == "Alice Example"


def test_merge_does_not_overwrite_name_with_none():
    """If a later event has no displayName but the cache already had a
    name, don't clobber the cached name."""
    existing = {
        "people": {
            "alice@example.com": {
                "name": "Alice Example",
                "last_seen": "2026-05-10",
                "sources": ["calendar-scan"],
            }
        }
    }
    sra.merge_attendees(
        existing,
        [("alice@example.com", None)],
        event_date="2026-05-15",
        requester_email=None,
    )
    assert existing["people"]["alice@example.com"]["name"] == "Alice Example"


def test_merge_dedupes_sources_list():
    existing = {
        "people": {
            "alice@example.com": {
                "name": "Alice",
                "last_seen": "2026-05-10",
                "sources": ["calendar-scan", "glean"],
            }
        }
    }
    sra.merge_attendees(
        existing,
        [("alice@example.com", "Alice")],
        event_date="2026-05-15",
        requester_email=None,
    )
    assert existing["people"]["alice@example.com"]["sources"] == ["calendar-scan", "glean"]


# ---------------------------------------------------------------------------
# load_existing — defensive on disk shape
# ---------------------------------------------------------------------------


def test_load_existing_missing_file_returns_skeleton(tmp_path):
    out = sra.load_existing(tmp_path / "people.yaml")
    assert out == {"people": {}}


def test_load_existing_empty_file_returns_skeleton(tmp_path):
    p = tmp_path / "people.yaml"
    p.write_text("")
    assert sra.load_existing(p) == {"people": {}}


def test_load_existing_corrupt_yaml_returns_skeleton(tmp_path, capsys):
    p = tmp_path / "people.yaml"
    p.write_text("people: {not: closed\n")
    out = sra.load_existing(p)
    assert out == {"people": {}}
    assert "malformed" in capsys.readouterr().err


def test_load_existing_template_only_returns_skeleton(tmp_path):
    """The committed template has `people: {}` — load that cleanly."""
    p = tmp_path / "people.yaml"
    p.write_text("people: {}\n")
    assert sra.load_existing(p) == {"people": {}}


# ---------------------------------------------------------------------------
# scan — end-to-end with mocked service
# ---------------------------------------------------------------------------


def _fake_service(events: list[dict], primary_email: str | None = "you@example.com") -> mock.MagicMock:
    """Build a stand-in for the Calendar discovery service. Mirrors the
    pattern in test_create_event.py + test_get_event.py."""
    svc = mock.MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {
        "items": events,
        # No nextPageToken — single page.
    }
    if primary_email is None:
        from googleapiclient.errors import HttpError
        svc.calendarList.return_value.get.return_value.execute.side_effect = HttpError(
            mock.MagicMock(status=403), b'{"error":"denied"}'
        )
    else:
        svc.calendarList.return_value.get.return_value.execute.return_value = {"id": primary_email}
    return svc


def test_scan_writes_people_yaml(tmp_path, monkeypatch):
    """End-to-end: events from the mocked service → people.yaml on disk."""
    events = [
        {
            "status": "confirmed",
            "start": {"dateTime": "2026-05-15T10:00:00-07:00"},
            "attendees": [
                {"email": "you@example.com", "displayName": "Example User"},
                {"email": "alice@example.com", "displayName": "Alice Example"},
                {"email": "bob@example.com", "displayName": "Bob Example"},
            ],
        },
    ]
    svc = _fake_service(events)
    out = tmp_path / "people.yaml"

    monkeypatch.setattr(sra.auth, "get_credentials", lambda *a, **kw: mock.MagicMock())
    monkeypatch.setattr(sra, "build", lambda *a, **kw: svc)

    summary = sra.scan(since_days=30, max_attendees=5, out_path=out, dry_run=False)

    assert summary["events_scanned"] == 1
    assert summary["events_used"] == 1
    assert summary["people_added"] == 2  # alice + bob (requester skipped)
    on_disk = yaml.safe_load(out.read_text())
    assert set(on_disk["people"].keys()) == {"alice@example.com", "bob@example.com"}
    assert on_disk["people"]["alice@example.com"]["name"] == "Alice Example"


def test_scan_dry_run_leaves_file_untouched(tmp_path, monkeypatch):
    events = [{
        "status": "confirmed",
        "start": {"dateTime": "2026-05-15T10:00:00-07:00"},
        "attendees": [
            {"email": "alice@example.com", "displayName": "Alice Example"},
        ],
    }]
    svc = _fake_service(events)
    out = tmp_path / "people.yaml"

    monkeypatch.setattr(sra.auth, "get_credentials", lambda *a, **kw: mock.MagicMock())
    monkeypatch.setattr(sra, "build", lambda *a, **kw: svc)

    summary = sra.scan(since_days=30, max_attendees=5, out_path=out, dry_run=True)
    assert summary["people_added"] == 1
    assert not out.exists()


def test_scan_paginates_through_multiple_pages(tmp_path, monkeypatch):
    """events.list returns nextPageToken; scanner keeps paging until
    it's exhausted."""
    pages = [
        {
            "items": [{
                "status": "confirmed",
                "start": {"dateTime": "2026-05-10T10:00:00-07:00"},
                "attendees": [{"email": "alice@example.com", "displayName": "Alice"}],
            }],
            "nextPageToken": "p2",
        },
        {
            "items": [{
                "status": "confirmed",
                "start": {"dateTime": "2026-05-15T10:00:00-07:00"},
                "attendees": [{"email": "bob@example.com", "displayName": "Bob"}],
            }],
        },
    ]
    svc = mock.MagicMock()
    svc.events.return_value.list.return_value.execute.side_effect = pages
    svc.calendarList.return_value.get.return_value.execute.return_value = {"id": "you@example.com"}

    monkeypatch.setattr(sra.auth, "get_credentials", lambda *a, **kw: mock.MagicMock())
    monkeypatch.setattr(sra, "build", lambda *a, **kw: svc)

    out = tmp_path / "people.yaml"
    summary = sra.scan(since_days=30, max_attendees=5, out_path=out, dry_run=False)
    assert summary["events_scanned"] == 2
    assert summary["people_added"] == 2


def test_scan_continues_when_primary_email_lookup_denied(tmp_path, monkeypatch):
    """When the token doesn't have the calendar.readonly scope needed
    for calendarList.get, scan still works — we just can't filter the
    requester from the people cache (a minor cost)."""
    events = [{
        "status": "confirmed",
        "start": {"dateTime": "2026-05-15T10:00:00-07:00"},
        "attendees": [
            {"email": "alice@example.com", "displayName": "Alice Example"},
        ],
    }]
    svc = _fake_service(events, primary_email=None)

    monkeypatch.setattr(sra.auth, "get_credentials", lambda *a, **kw: mock.MagicMock())
    monkeypatch.setattr(sra, "build", lambda *a, **kw: svc)

    out = tmp_path / "people.yaml"
    summary = sra.scan(since_days=30, max_attendees=5, out_path=out, dry_run=False)
    assert summary["people_added"] == 1
