"""Tests for events_around — fetches one attendee's own events in a
±N-hour window around a slot for the context timeline. Pure-function
coverage of classify_and_shape (filters that match the group
timeline's filters); mocked-service coverage of the fetcher."""

from __future__ import annotations

import datetime as dt
from unittest import mock

import events_around as ea


# ---------------------------------------------------------------------------
# classify_and_shape — filters + key mapping
# ---------------------------------------------------------------------------


def _ev(
    *,
    summary: str | None = "Meeting",
    start: str = "2026-05-22T14:30:00-07:00",
    end: str = "2026-05-22T15:00:00-07:00",
    status: str = "confirmed",
    transparency: str | None = None,
    event_type: str | None = None,
    attendees: list[dict] | None = None,
    recurring_event_id: str | None = None,
) -> dict:
    e: dict = {
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if transparency:
        e["transparency"] = transparency
    if event_type:
        e["eventType"] = event_type
    if attendees is not None:
        e["attendees"] = attendees
    if recurring_event_id:
        e["recurringEventId"] = recurring_event_id
    return e


def test_classify_shape_returns_render_slot_compatible_dict():
    """The shape returned must match the inner `conflict` shape that
    render_slot.tick_glyph reads (visible, movability, conflict_start,
    conflict_end at minimum)."""
    out = ea.classify_and_shape(_ev(summary="Alex 1:1"))
    assert out is not None
    for required in ("visible", "summary", "movability", "category",
                     "conflict_start", "conflict_end"):
        assert required in out, f"missing key {required}"
    assert out["summary"] == "Alex 1:1"
    assert out["conflict_start"] == "2026-05-22T14:30:00-07:00"


def test_classify_shape_filters_cancelled():
    assert ea.classify_and_shape(_ev(status="cancelled")) is None


def test_classify_shape_filters_transparent():
    """Events explicitly marked 'show me as available' don't block —
    they shouldn't render as conflicts in context either."""
    assert ea.classify_and_shape(_ev(transparency="transparent")) is None


def test_classify_shape_filters_declined_by_self():
    """If the requester declined the invite, the event isn't on their
    blocking calendar."""
    assert (
        ea.classify_and_shape(
            _ev(
                attendees=[
                    {"email": "alice@example.com", "self": True, "responseStatus": "declined"},
                    {"email": "bob@example.com"},
                ]
            )
        )
        is None
    )


def test_classify_shape_filters_working_location():
    """Working-location pseudo-events are just office-vs-home markers."""
    assert ea.classify_and_shape(_ev(event_type="workingLocation")) is None


def test_classify_shape_filters_all_day():
    """All-day events (PTO, holidays) skew per-hour timelines and
    aren't conflicts at the 30-min-tick resolution we render context at."""
    ev = {
        "summary": "PTO",
        "status": "confirmed",
        "start": {"date": "2026-05-22"},
        "end": {"date": "2026-05-23"},
    }
    assert ea.classify_and_shape(ev) is None


def test_classify_shape_marks_opaque_event_as_not_visible():
    """No summary string (free/busy-only sharing) → visible=False so
    the renderer picks the '?' glyph just as it does for group conflicts."""
    out = ea.classify_and_shape(_ev(summary=None))
    assert out is not None
    assert out["visible"] is False


def test_classify_shape_carries_recurring_flag():
    out = ea.classify_and_shape(_ev(recurring_event_id="abc_R20260522T140000"))
    assert out is not None
    assert out["recurring"] is True


# ---------------------------------------------------------------------------
# events_around — windowing + paging + service mock
# ---------------------------------------------------------------------------


def _fake_service(items_pages: list[list[dict]]) -> mock.MagicMock:
    """Build a stand-in that returns `items_pages[i]` on the i'th call,
    with nextPageToken set for every page except the last."""
    svc = mock.MagicMock()
    responses = []
    for i, items in enumerate(items_pages):
        resp = {"items": items}
        if i < len(items_pages) - 1:
            resp["nextPageToken"] = f"p{i+1}"
        responses.append(resp)
    svc.events.return_value.list.return_value.execute.side_effect = responses
    return svc


def test_events_around_window_padding():
    """time_min/time_max bracket the slot by hours_before/after."""
    svc = _fake_service([[]])
    ea.events_around(
        svc,
        calendar_id="you@example.com",
        slot_start=dt.datetime.fromisoformat("2026-05-22T14:30:00-07:00"),
        slot_end=dt.datetime.fromisoformat("2026-05-22T15:15:00-07:00"),
        hours_before=2.0,
        hours_after=2.0,
    )
    list_kwargs = svc.events.return_value.list.call_args.kwargs
    # 2h before 14:30 = 12:30; 2h after 15:15 = 17:15
    assert list_kwargs["timeMin"].startswith("2026-05-22T12:30:00")
    assert list_kwargs["timeMax"].startswith("2026-05-22T17:15:00")
    assert list_kwargs["calendarId"] == "you@example.com"
    assert list_kwargs["singleEvents"] is True


def test_events_around_pages_through_results():
    """When the API returns nextPageToken, the fetcher follows it."""
    page1 = [_ev(summary="Early")]
    page2 = [_ev(summary="Later", start="2026-05-22T15:30:00-07:00", end="2026-05-22T16:00:00-07:00")]
    svc = _fake_service([page1, page2])
    out = ea.events_around(
        svc,
        calendar_id="you@example.com",
        slot_start=dt.datetime.fromisoformat("2026-05-22T14:30:00-07:00"),
        slot_end=dt.datetime.fromisoformat("2026-05-22T15:15:00-07:00"),
        hours_before=2.0,
        hours_after=2.0,
    )
    summaries = [e["summary"] for e in out]
    assert summaries == ["Early", "Later"]


def test_events_around_drops_filtered_events_from_output():
    """Cancelled / transparent / all-day events are filtered out."""
    svc = _fake_service([[
        _ev(summary="Real meeting"),
        _ev(summary="Cancelled", status="cancelled"),
        _ev(summary="Transparent", transparency="transparent"),
    ]])
    out = ea.events_around(
        svc,
        calendar_id="you@example.com",
        slot_start=dt.datetime.fromisoformat("2026-05-22T14:30:00-07:00"),
        slot_end=dt.datetime.fromisoformat("2026-05-22T15:15:00-07:00"),
        hours_before=2.0,
        hours_after=2.0,
    )
    assert [e["summary"] for e in out] == ["Real meeting"]


def test_events_around_fractional_hours():
    """Sub-hour windows work (e.g. 0.5 = 30 min before/after)."""
    svc = _fake_service([[]])
    ea.events_around(
        svc,
        calendar_id="you@example.com",
        slot_start=dt.datetime.fromisoformat("2026-05-22T14:30:00-07:00"),
        slot_end=dt.datetime.fromisoformat("2026-05-22T15:15:00-07:00"),
        hours_before=0.5,
        hours_after=0.5,
    )
    list_kwargs = svc.events.return_value.list.call_args.kwargs
    assert list_kwargs["timeMin"].startswith("2026-05-22T14:00:00")
    assert list_kwargs["timeMax"].startswith("2026-05-22T15:45:00")
