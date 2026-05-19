"""Tests for book_browser_helpers — pure helpers that back the SKILL.md
Playwright booking recipe. No browser/MCP calls touched here; the helpers
are deterministic transforms on time, URL, and snapshot strings."""

from __future__ import annotations

import base64
import datetime as dt

import book_browser_helpers as h


# ---------------------------------------------------------------------------
# format_time_picker


class TestFormatTimePicker:
    def test_morning_round_hour(self):
        assert h.format_time_picker(dt.time(9, 0)) == "9:00 AM"

    def test_afternoon_half_past(self):
        assert h.format_time_picker(dt.time(13, 30)) == "1:30 PM"

    def test_midnight(self):
        assert h.format_time_picker(dt.time(0, 0)) == "12:00 AM"

    def test_just_after_midnight(self):
        assert h.format_time_picker(dt.time(0, 15)) == "12:15 AM"

    def test_noon(self):
        assert h.format_time_picker(dt.time(12, 0)) == "12:00 PM"

    def test_just_after_noon(self):
        assert h.format_time_picker(dt.time(12, 45)) == "12:45 PM"

    def test_late_evening(self):
        assert h.format_time_picker(dt.time(23, 5)) == "11:05 PM"

    def test_accepts_datetime(self):
        assert h.format_time_picker(dt.datetime(2026, 1, 1, 14, 5)) == "2:05 PM"

    def test_pads_single_digit_minute(self):
        # The picker insists on two-digit minutes; if we ever drop the
        # zfill, Calendar interprets "9:5 AM" as "9:05 AM" but emits a
        # state-change event that breaks our re-snapshot assertions.
        assert h.format_time_picker(dt.time(9, 5)) == "9:05 AM"


# ---------------------------------------------------------------------------
# format_date_picker


class TestFormatDatePicker:
    def test_basic(self):
        assert h.format_date_picker(dt.date(2026, 5, 22)) == "May 22, 2026"

    def test_january_single_digit_day(self):
        assert h.format_date_picker(dt.date(2026, 1, 3)) == "Jan 3, 2026"

    def test_december_double_digit_day(self):
        assert h.format_date_picker(dt.date(2026, 12, 14)) == "Dec 14, 2026"

    def test_accepts_datetime(self):
        assert h.format_date_picker(dt.datetime(2026, 12, 1, 9, 0)) == "Dec 1, 2026"


# ---------------------------------------------------------------------------
# extract_eid_from_url


class TestExtractEidFromUrl:
    REAL_EID = "cGpjOXNoMGFtbmhqMDBoa2V1YXI0MDBndjQgbXNlYWxAY29uZmx1ZW50Lmlv"

    def test_query_param_form(self):
        url = f"https://www.google.com/calendar/event?eid={self.REAL_EID}"
        assert h.extract_eid_from_url(url) == self.REAL_EID

    def test_calendar_dot_google_query_param(self):
        url = f"https://calendar.google.com/calendar/event?eid={self.REAL_EID}&ctz=America/Los_Angeles"
        assert h.extract_eid_from_url(url) == self.REAL_EID

    def test_eventedit_path_form(self):
        url = f"https://calendar.google.com/calendar/u/0/r/eventedit/{self.REAL_EID}"
        assert h.extract_eid_from_url(url) == self.REAL_EID

    def test_no_eid_returns_none(self):
        assert h.extract_eid_from_url("https://calendar.google.com/") is None

    def test_unrelated_url_returns_none(self):
        assert h.extract_eid_from_url("https://example.com/?eid=") is None


# ---------------------------------------------------------------------------
# decode_eid


class TestDecodeEid:
    def test_round_trip_with_padding_stripped(self):
        # Calendar emits eids without trailing '=' padding; helper must re-pad.
        raw = "pjc9sh0amnhj00hkeuar400gv4 you@example.com"
        eid = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        assert h.decode_eid(eid) == ("pjc9sh0amnhj00hkeuar400gv4", "you@example.com")

    def test_round_trip_with_padding_kept(self):
        raw = "abc123 someone@example.com"
        eid = base64.urlsafe_b64encode(raw.encode()).decode()
        assert h.decode_eid(eid) == ("abc123", "someone@example.com")

    def test_invalid_base64_returns_none(self):
        assert h.decode_eid("not-base64-at-all-$$$") is None

    def test_no_space_separator_returns_none(self):
        eid = base64.urlsafe_b64encode(b"justaneventid").decode().rstrip("=")
        assert h.decode_eid(eid) is None

    def test_empty_event_id_returns_none(self):
        eid = base64.urlsafe_b64encode(b" you@example.com").decode().rstrip("=")
        assert h.decode_eid(eid) is None

    def test_empty_email_returns_none(self):
        eid = base64.urlsafe_b64encode(b"eventid ").decode().rstrip("=")
        assert h.decode_eid(eid) is None


# ---------------------------------------------------------------------------
# extract_zoom_url


class TestExtractZoomUrl:
    def test_regular_meeting_with_pwd(self):
        text = "Joining info: https://confluent.zoom.us/j/1234567890?pwd=abc Click here"
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/j/1234567890?pwd=abc"

    def test_personal_room(self):
        text = "Zoom https://confluent.zoom.us/my/you more info"
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/my/you"

    def test_webinar_form(self):
        text = "https://confluent.zoom.us/w/987654 trailing"
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/w/987654"

    def test_trims_trailing_period(self):
        text = "Click https://confluent.zoom.us/j/123?pwd=xyz."
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/j/123?pwd=xyz"

    def test_trims_trailing_paren(self):
        text = "Join (https://confluent.zoom.us/j/123?pwd=xyz)"
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/j/123?pwd=xyz"

    def test_no_match_returns_none(self):
        assert h.extract_zoom_url("plain text with no zoom link") is None

    def test_non_zoom_url_ignored(self):
        # Make sure we don't pick up a meet.google.com URL or similar.
        assert h.extract_zoom_url("https://meet.google.com/abc-defg-hij") is None

    def test_first_match_wins(self):
        # When the snapshot contains multiple Zoom URLs (e.g., the
        # join URL and a passcode-info URL), take the first one,
        # which is conventionally the join URL.
        text = (
            "Join: https://confluent.zoom.us/j/111?pwd=aaa "
            "Backup: https://confluent.zoom.us/j/222?pwd=bbb"
        )
        assert h.extract_zoom_url(text) == "https://confluent.zoom.us/j/111?pwd=aaa"


# ---------------------------------------------------------------------------
# shape_response


class TestShapeResponse:
    def _make_args(self, **overrides) -> dict:
        base = dict(
            event_id="evt123",
            html_link="https://calendar.google.com/calendar/event?eid=foo",
            zoom_url="https://confluent.zoom.us/j/1234567890?pwd=abc",
            summary="Test sync",
            start=dt.datetime(2026, 5, 22, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7))),
            end=dt.datetime(2026, 5, 22, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-7))),
            attendees=["you@example.com", "eve@example.com"],
        )
        base.update(overrides)
        return base

    def test_shape_with_zoom_url(self):
        r = h.shape_response(**self._make_args())
        assert r["event_id"] == "evt123"
        assert r["html_link"] == "https://calendar.google.com/calendar/event?eid=foo"
        assert r["join_url"] == "https://confluent.zoom.us/j/1234567890?pwd=abc"
        assert r["conference_solution"] == "Zoom Meeting"
        assert "browser path" in r["conference_status"]
        assert r["summary"] == "Test sync"
        assert r["attendees"] == ["you@example.com", "eve@example.com"]
        assert r["start"].startswith("2026-05-22T09:00:00")
        assert r["end"].startswith("2026-05-22T09:30:00")

    def test_shape_without_zoom_url(self):
        r = h.shape_response(**self._make_args(zoom_url=None))
        assert r["join_url"] is None
        assert r["conference_solution"] is None
        assert r["conference_status"] == "no Zoom URL captured"

    def test_attendees_is_a_copy(self):
        # If the caller mutates their input list after calling, the
        # response dict shouldn't reflect that — defensive copy.
        attendees = ["a@x.com"]
        r = h.shape_response(**self._make_args(attendees=attendees))
        attendees.append("b@x.com")
        assert r["attendees"] == ["a@x.com"]

    def test_matches_api_path_keys(self):
        # The whole reason this helper exists is shape-parity with
        # create_event.summarize_response. Lock in the key set so a
        # future drift gets caught here.
        r = h.shape_response(**self._make_args())
        expected_keys = {
            "event_id",
            "html_link",
            "join_url",
            "conference_solution",
            "conference_status",
            "attendees",
            "start",
            "end",
            "summary",
        }
        assert set(r.keys()) == expected_keys
