"""Tests for the conflict classifier and scoring logic in freebusy.py.

The classifier maps Google Calendar events to a movability score 0-10 plus
a category label. It has had real bugs (the "OOO Travel" mis-classification)
so the corpus here is meant to lock in those fixes.

Pure-function tests only — we don't hit any Google API.
"""

from __future__ import annotations

import datetime as dt

import pytest
import freebusy as fb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(
    summary=None,
    start="2026-05-20T10:00:00-07:00",
    end="2026-05-20T11:00:00-07:00",
    status="confirmed",
    transparency="opaque",
    event_type="default",
    attendees=None,
    recurring=False,
    all_day=False,
):
    ev = {"status": status, "transparency": transparency, "eventType": event_type}
    if summary is not None:
        ev["summary"] = summary
    if all_day:
        ev["start"] = {"date": start[:10]}
        ev["end"] = {"date": end[:10]}
    else:
        ev["start"] = {"dateTime": start}
        ev["end"] = {"dateTime": end}
    if attendees:
        ev["attendees"] = attendees
    if recurring:
        ev["recurringEventId"] = "rec123"
    return ev


def _dt(s):
    return dt.datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# classify_event — title-rule corpus
# ---------------------------------------------------------------------------

def test_classify_ooo_beats_travel_ordering():
    """Regression test: 'DNS - OOO Travel to NYC' must categorize as ooo
    (movability 0), not travel_block (7). The rule order in TITLE_RULES
    must put OOO before travel for this to hold."""
    cls = fb.classify_event(_ev(summary="DNS - OOO Travel to NYC"))
    assert cls["category"] == "ooo"
    assert cls["movability"] == 0


def test_classify_dns_alone_is_ooo():
    """DNS / DNB / 'Do Not Schedule' patterns → ooo."""
    assert fb.classify_event(_ev(summary="DNS"))["category"] == "ooo"
    assert fb.classify_event(_ev(summary="DNB this slot"))["category"] == "ooo"
    assert fb.classify_event(_ev(summary="Do not schedule"))["category"] == "ooo"


def test_classify_focus_block_is_max_movable():
    assert fb.classify_event(_ev(summary="Focus time"))["movability"] == 10
    assert fb.classify_event(_ev(summary="Deep work"))["movability"] == 10
    assert fb.classify_event(_ev(summary="Heads down"))["movability"] == 10


def test_classify_one_on_one_patterns():
    """Various 1:1 patterns all categorize the same way."""
    for title in ("1:1 with Alice", "Alice/Bob 1:1", "Alice 1-1 Bob", "One on one"):
        cls = fb.classify_event(_ev(summary=title))
        assert cls["category"] == "one_on_one", f"failed for {title!r}"
        assert cls["movability"] == 8


def test_classify_customer_meeting_is_low_movability():
    assert fb.classify_event(_ev(summary="Customer call with Acme"))["movability"] == 2
    assert fb.classify_event(_ev(summary="Client sync"))["movability"] == 2


def test_classify_interview_is_immovable():
    assert fb.classify_event(_ev(summary="Phone screen - John Doe"))["movability"] == 1
    assert fb.classify_event(_ev(summary="Interview loop"))["movability"] == 1


def test_classify_all_hands_is_immovable():
    assert fb.classify_event(_ev(summary="All-hands"))["movability"] == 1
    assert fb.classify_event(_ev(summary="Town hall"))["movability"] == 1


def test_classify_event_type_out_of_office_overrides_title():
    """eventType=outOfOffice forces ooo regardless of summary."""
    cls = fb.classify_event(_ev(summary="Looks like a meeting", event_type="outOfOffice"))
    assert cls["category"] == "ooo"
    assert cls["movability"] == 0


def test_classify_event_type_focus_time():
    cls = fb.classify_event(_ev(summary="Anything", event_type="focusTime"))
    assert cls["category"] == "focus_block"
    assert cls["movability"] == 10


def test_classify_opaque_when_summary_hidden():
    """No 'summary' key (Workspace's free/busy-only sharing) → opaque category
    with a neutral movability so Claude has to ask the attendee."""
    cls = fb.classify_event(_ev(summary=None))
    assert cls["category"] == "opaque"
    assert cls["visible"] is False
    assert cls["movability"] == 5


def test_classify_large_attendee_count_de_rates_movability():
    """A team meeting with 27 attendees should be harder to move than one
    with 4 (coordination cost). Movability gets clamped down by 2."""
    small = fb.classify_event(_ev(summary="Team sync", attendees=[{"email": f"a{i}@x"} for i in range(4)]))
    large = fb.classify_event(_ev(summary="Team sync", attendees=[{"email": f"a{i}@x"} for i in range(27)]))
    assert small["movability"] == 5
    assert large["movability"] == 3  # 5 - 2 = 3


def test_classify_tentative_status_boosts_movability():
    """status=tentative slightly weakens a 'fixed' classification — if you
    haven't even committed, others can probably move you."""
    confirmed = fb.classify_event(_ev(summary="Customer call", status="confirmed"))
    tentative = fb.classify_event(_ev(summary="Customer call", status="tentative"))
    assert tentative["movability"] > confirmed["movability"]


def test_classify_all_day_generic_becomes_block():
    """All-day events with generic titles count as 'all_day_block' — usually
    OOO or hold-the-day even if not explicitly labeled."""
    cls = fb.classify_event(_ev(summary="random thing", all_day=True))
    assert cls["category"] == "all_day_block"
    assert cls["movability"] == 1


# ---------------------------------------------------------------------------
# event_blocks_time — which events actually conflict
# ---------------------------------------------------------------------------

def test_transparent_event_does_not_block():
    """transparency=transparent means 'I'm available during this event' even
    if it's on the calendar — don't count as a conflict."""
    ev = _ev(summary="Optional FYI", transparency="transparent")
    cls = fb.classify_event(ev)
    assert fb.event_blocks_time(ev, cls) is False


def test_declined_self_event_does_not_block():
    """If I declined an invite, I'm not actually committed to that time."""
    ev = _ev(
        summary="Some meeting",
        attendees=[{"email": "me@x", "self": True, "responseStatus": "declined"}],
    )
    cls = fb.classify_event(ev)
    assert fb.event_blocks_time(ev, cls) is False


def test_accepted_self_event_blocks():
    ev = _ev(
        summary="Some meeting",
        attendees=[{"email": "me@x", "self": True, "responseStatus": "accepted"}],
    )
    cls = fb.classify_event(ev)
    assert fb.event_blocks_time(ev, cls) is True


# ---------------------------------------------------------------------------
# score_slot — penalty math
# ---------------------------------------------------------------------------

def test_score_no_conflicts_no_edge_no_lunch():
    """Mid-day slot, no conflicts, no edges → score 100."""
    start = _dt("2026-05-20T14:00:00-07:00")  # Wed 2pm
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17))
    assert result["score"] == 100
    assert result["breakdown"] == []


def test_score_lunch_overlap_deducts_10():
    start = _dt("2026-05-20T12:30:00-07:00")  # spans lunch
    end = _dt("2026-05-20T13:30:00-07:00")
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17))
    assert result["score"] == 90
    assert any("lunch" in b["label"] for b in result["breakdown"])


def test_score_first_30_min_deducts_5():
    start = _dt("2026-05-20T09:00:00-07:00")
    end = _dt("2026-05-20T09:30:00-07:00")
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17))
    assert result["score"] == 95


def test_score_last_30_min_deducts_5():
    start = _dt("2026-05-20T16:30:00-07:00")
    end = _dt("2026-05-20T17:00:00-07:00")
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17))
    assert result["score"] == 95


def test_score_one_conflict_movability_8():
    """One conflict with movability 8 → penalty (10-8)*5 = 10."""
    conflicts = [{"attendee": "alice@x", "conflict": {"movability": 8, "category": "one_on_one"}}]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    assert result["score"] == 90


def test_score_one_conflict_ooo_caps_at_50_penalty():
    """OOO (movability 0) → penalty (10-0)*5 = 50."""
    conflicts = [{"attendee": "alice@x", "conflict": {"movability": 0, "category": "ooo"}}]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    assert result["score"] == 50


def test_score_floors_at_zero():
    """Score never goes negative even with stacked penalties."""
    conflicts = [
        {"attendee": "a@x", "conflict": {"movability": 0, "category": "ooo"}},
        {"attendee": "b@x", "conflict": {"movability": 0, "category": "ooo"}},
        {"attendee": "c@x", "conflict": {"movability": 0, "category": "ooo"}},
    ]
    start = _dt("2026-05-20T09:00:00-07:00")  # also day-edge early
    end = _dt("2026-05-20T09:30:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    assert result["score"] == 0


# ---------------------------------------------------------------------------
# dedup_and_rank — conflict signature collisions
# ---------------------------------------------------------------------------

def test_dedup_drops_same_conflict_signature():
    """Two slots that share the same set of (attendee, conflict event) pairs
    represent the same trade-off — only one should be returned."""
    sig_match_a = {
        "start": "2026-05-20T13:00:00-07:00",
        "end":   "2026-05-20T13:30:00-07:00",
        "score": 60,
        "conflicts": [
            {"attendee": "alice@x", "conflict": {"summary": "1:1 with Bob", "category": "one_on_one", "movability": 8}}
        ],
    }
    sig_match_b = {
        "start": "2026-05-20T14:00:00-07:00",
        "end":   "2026-05-20T14:30:00-07:00",
        "score": 60,
        "conflicts": [
            {"attendee": "alice@x", "conflict": {"summary": "1:1 with Bob", "category": "one_on_one", "movability": 8}}
        ],
    }
    different = {
        "start": "2026-05-20T15:00:00-07:00",
        "end":   "2026-05-20T15:30:00-07:00",
        "score": 60,
        "conflicts": [
            {"attendee": "bob@x", "conflict": {"summary": "Standup", "category": "team_standup", "movability": 5}}
        ],
    }
    out = fb.dedup_and_rank([sig_match_a, sig_match_b, different], top_n=5)
    # Same-sig pair should collapse to one; different stays
    assert len(out) == 2
    sigs = {fb.conflict_signature(s) for s in out}
    assert len(sigs) == 2


def test_dedup_allows_multiple_all_free_slots():
    """All-free slots (empty conflict signature) shouldn't dedupe against each other."""
    slots = [
        {"start": "2026-05-20T10:00:00-07:00", "end": "2026-05-20T10:30:00-07:00", "score": 100, "conflicts": []},
        {"start": "2026-05-20T14:00:00-07:00", "end": "2026-05-20T14:30:00-07:00", "score": 100, "conflicts": []},
        {"start": "2026-05-21T10:00:00-07:00", "end": "2026-05-21T10:30:00-07:00", "score": 100, "conflicts": []},
    ]
    out = fb.dedup_and_rank(slots, top_n=5)
    assert len(out) == 3


def test_dedup_drops_overlapping_slots():
    """Even with different conflict signatures, two slots that overlap in
    time should not both be selected — pick the higher-scoring one."""
    higher = {"start": "2026-05-20T10:00:00-07:00", "end": "2026-05-20T11:00:00-07:00", "score": 95, "conflicts": []}
    overlapping_lower = {"start": "2026-05-20T10:15:00-07:00", "end": "2026-05-20T11:15:00-07:00", "score": 80,
                         "conflicts": [{"attendee": "x@y", "conflict": {"summary": "A", "category": "team_meeting", "movability": 5}}]}
    non_overlapping = {"start": "2026-05-20T14:00:00-07:00", "end": "2026-05-20T15:00:00-07:00", "score": 90, "conflicts": []}
    out = fb.dedup_and_rank([higher, overlapping_lower, non_overlapping], top_n=5)
    starts = sorted(s["start"] for s in out)
    assert "2026-05-20T10:15:00-07:00" not in starts  # the overlapping lower-scored one is dropped


# ---------------------------------------------------------------------------
# load_working_hours — YAML parsing
# ---------------------------------------------------------------------------

def test_load_working_hours_missing_file_uses_defaults(tmp_path):
    """No config file → every weekday is 09:00-17:00."""
    wh = fb.load_working_hours(tmp_path / "does-not-exist.yaml")
    for day in fb.DAY_NAMES:
        ws, we = wh[day]
        assert (ws, we) == (dt.time(9), dt.time(17))


def test_load_working_hours_per_day_override(tmp_path):
    """Per-day override merges over the default."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "working_hours:\n"
        "  default: \"09:00-17:00\"\n"
        "  friday:  \"09:00-15:00\"\n"
    )
    wh = fb.load_working_hours(cfg)
    assert wh["monday"] == (dt.time(9), dt.time(17))
    assert wh["friday"] == (dt.time(9), dt.time(15))


def test_load_working_hours_string_form(tmp_path):
    """working_hours can also be a plain string (shortcut for default)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("working_hours: \"10:00-18:00\"\n")
    wh = fb.load_working_hours(cfg)
    assert wh["wednesday"] == (dt.time(10), dt.time(18))
