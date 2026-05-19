"""Tests for get_event.py — the events.get wrapper used by the hybrid
booking path to confirm what conferenceData was attached after the
Playwright Zoom-add-on click step. Mocks the Calendar service at the
fetch_event() boundary."""

from __future__ import annotations

from unittest import mock

import get_event as ge


# A realistic shape from events.get after the Zoom add-on dispatched —
# includes all the entry points the real add-on populates (video, US
# phone, intl phone, SIP, "more").
ZOOM_EVENT = {
    "id": "7jsh4tkejrc62cai0mhrfj0ut4",
    "summary": "Plan B test — real Zoom add-on",
    "htmlLink": "https://www.google.com/calendar/event?eid=N2pzaDR0a2VqcmM2MmNhaTBtaHJmajB1dDQg",
    "start": {"dateTime": "2026-05-18T16:30:00-07:00"},
    "end": {"dateTime": "2026-05-18T17:00:00-07:00"},
    "attendees": [{"email": "you@example.com"}],
    "conferenceData": {
        "conferenceSolution": {"name": "Zoom Meeting", "key": {"type": "addOn"}},
        "entryPoints": [
            {"entryPointType": "video", "uri": "https://confluent.zoom.us/j/95101148919?pwd=xyz"},
            {"entryPointType": "phone", "uri": "tel:+16699006833,,95101148919#"},
            {"entryPointType": "sip", "uri": "sip:95101148919@zoomcrc.com"},
        ],
    },
}

BARE_EVENT = {
    "id": "abc123",
    "summary": "No conferencing here",
    "htmlLink": "https://www.google.com/calendar/event?eid=foo",
    "start": {"dateTime": "2026-05-19T09:00:00-07:00"},
    "end": {"dateTime": "2026-05-19T09:30:00-07:00"},
    "attendees": [],
}


def _fake_service(event: dict) -> mock.MagicMock:
    """Build a stand-in for the discovery service that returns `event`
    from `service.events().get(...).execute()`. Mirrors the
    test_create_event.py pattern."""
    svc = mock.MagicMock()
    svc.events.return_value.get.return_value.execute.return_value = event
    return svc


def test_fetch_event_calls_correct_boundary():
    """Verify fetch_event passes calendarId + eventId through to
    events().get() — guards against accidental signature drift."""
    svc = _fake_service(ZOOM_EVENT)
    result = ge.fetch_event(svc, "7jsh4tkejrc62cai0mhrfj0ut4", "primary")
    svc.events.return_value.get.assert_called_once_with(
        calendarId="primary", eventId="7jsh4tkejrc62cai0mhrfj0ut4"
    )
    assert result == ZOOM_EVENT


def test_fetch_event_respects_calendar_param():
    """Non-primary calendarId is forwarded — supports impersonation /
    shared calendars."""
    svc = _fake_service(ZOOM_EVENT)
    ge.fetch_event(svc, "ev1", "other-calendar@group.calendar.google.com")
    svc.events.return_value.get.assert_called_once_with(
        calendarId="other-calendar@group.calendar.google.com", eventId="ev1"
    )


def test_summarize_via_create_event_for_zoom_attached():
    """The shape comes from create_event.summarize_response — so the
    Zoom-attached case should produce the same 'attached: Zoom Meeting'
    status the API path emits for a successful zoom booking."""
    import create_event as ce
    summary = ce.summarize_response(ZOOM_EVENT, requested_conference="zoom")
    assert summary["event_id"] == "7jsh4tkejrc62cai0mhrfj0ut4"
    assert summary["join_url"] == "https://confluent.zoom.us/j/95101148919?pwd=xyz"
    assert summary["conference_solution"] == "Zoom Meeting"
    assert "attached" in summary["conference_status"]


def test_summarize_via_create_event_for_no_conference():
    """If Playwright failed to attach Zoom (the recoverable case), the
    re-query returns a bare event. requested_conference='zoom' tells
    summarize_response to produce the diagnostic 'requested zoom but no
    conference entry points attached' message, which the caller surfaces
    so the user can manually add Zoom in their own browser tab."""
    import create_event as ce
    summary = ce.summarize_response(BARE_EVENT, requested_conference="zoom")
    assert summary["join_url"] is None
    assert summary["conference_solution"] is None
    assert "no conference entry points" in summary["conference_status"]


def test_summarize_via_create_event_for_explicit_none():
    """When the create-time intent was 'none' (in-person/hold), the
    status is the neutral 'no conference requested' rather than the
    diagnostic message."""
    import create_event as ce
    summary = ce.summarize_response(BARE_EVENT, requested_conference="none")
    assert summary["join_url"] is None
    assert summary["conference_status"] == "no conference requested"
