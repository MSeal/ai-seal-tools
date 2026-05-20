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
    recurring_event_id="rec123",
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
        ev["recurringEventId"] = recurring_event_id
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


# Meals: lunch and coffee are midday/casual and reasonably shiftable.
# Breakfast and dinner sit at the bookends of the personal day and are
# anchored by family routine (school drop-off, kids' bedtime, partner's
# schedule) — treat as ~fixed so the scorer doesn't propose moving them.
# Regression on the original single-tier classifier (everything mapped
# to movability 6, which let "Dinner" look as easy to shift as "Lunch").

def test_classify_lunch_keeps_moderate_movability():
    cls = fb.classify_event(_ev(summary="Lunch"))
    assert cls["category"] == "meal"
    assert cls["movability"] == 6


def test_classify_coffee_keeps_moderate_movability():
    cls = fb.classify_event(_ev(summary="Coffee with Alice"))
    assert cls["category"] == "meal"
    assert cls["movability"] == 6


def test_classify_dinner_is_low_movability():
    cls = fb.classify_event(_ev(summary="Dinner"))
    assert cls["category"] == "meal"
    assert cls["movability"] == 3


def test_classify_breakfast_is_low_movability():
    cls = fb.classify_event(_ev(summary="Breakfast meeting"))
    assert cls["category"] == "meal"
    assert cls["movability"] == 3


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


# ---------------------------------------------------------------------------
# load_attendee_timezones + cross-attendee TZ scoring
# ---------------------------------------------------------------------------

def test_load_attendee_timezones_missing_file(tmp_path):
    """No config file → empty mapping (current behavior preserved)."""
    assert fb.load_attendee_timezones(tmp_path / "no.yaml") == {}


def test_load_attendee_timezones_normalizes_emails(tmp_path):
    """Email keys are lowercased so lookups are case-insensitive."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "attendee_timezones:\n"
        "  Alice@Example.COM: America/New_York\n"
        "  bob@example.com:   Europe/Berlin\n"
    )
    tzs = fb.load_attendee_timezones(cfg)
    assert tzs == {"alice@example.com": "America/New_York", "bob@example.com": "Europe/Berlin"}


def test_score_no_penalty_when_attendee_in_local_hours():
    """Slot at 10am PT; attendee in NY → 1pm Eastern, inside 9-17. No penalty."""
    start = _dt("2026-05-20T10:00:00-07:00")  # Wed 10am PT
    end = _dt("2026-05-20T11:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["alice@example.com"],
        attendee_timezones={"alice@example.com": "America/New_York"},
    )
    assert result["score"] == 100
    assert all("outside working hours" not in b["label"] for b in result["breakdown"])


def test_score_penalty_when_attendee_outside_local_hours():
    """Slot at 10am PT; attendee in Berlin → 7pm CEST, outside 9-17. -20."""
    start = _dt("2026-05-20T10:00:00-07:00")  # Wed 10am PT
    end = _dt("2026-05-20T11:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["bob@example.com"],
        attendee_timezones={"bob@example.com": "Europe/Berlin"},
    )
    assert result["score"] == 80
    tz_entries = [b for b in result["breakdown"] if "outside working hours" in b["label"]]
    assert len(tz_entries) == 1
    assert "bob@example.com" in tz_entries[0]["label"]
    assert "Europe/Berlin" in tz_entries[0]["label"]


def test_score_penalty_when_attendee_weekend_in_local_tz():
    """Slot at Fri 5pm PT = Sat 8am Tokyo. Attendee's weekend → penalty fires."""
    start = _dt("2026-05-22T17:00:00-07:00")  # Fri 5pm PT
    end = _dt("2026-05-22T18:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["carol@example.com"],
        attendee_timezones={"carol@example.com": "Asia/Tokyo"},
    )
    assert any("outside working hours" in b["label"] for b in result["breakdown"])


def test_score_unknown_attendee_tz_no_penalty():
    """Attendee with no TZ entry is scored as same-TZ-as-requester. No penalty."""
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["alice@example.com", "bob@example.com"],
        attendee_timezones={"alice@example.com": "America/New_York"},  # bob missing
    )
    # alice in NY at 1pm = OK. bob has no entry → no TZ penalty.
    assert result["score"] == 100


def test_score_invalid_tz_name_silently_skipped():
    """A typo'd or unknown IANA name is logged as a skip, not a crash."""
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["alice@example.com"],
        attendee_timezones={"alice@example.com": "Not/A/Real_Zone"},
    )
    # No crash, no penalty applied.
    assert result["score"] == 100


def test_score_email_lookup_is_case_insensitive():
    """Attendee email in mixed case still matches the lowercased TZ map."""
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["Bob@Example.COM"],  # passed in mixed case
        attendee_timezones={"bob@example.com": "Europe/Berlin"},  # stored lowercased
    )
    assert any("outside working hours" in b["label"] for b in result["breakdown"])


# ---------------------------------------------------------------------------
# ScoreWeights config + custom-weight application in score_slot
# ---------------------------------------------------------------------------

def test_score_weights_load_no_files_uses_defaults(tmp_path):
    """No defaults file + no override → dataclass defaults."""
    w = fb.ScoreWeights.load(tmp_path / "missing.yaml", tmp_path / "missing.local.yaml")
    assert w == fb.ScoreWeights()  # all fields at their dataclass defaults


def test_score_weights_load_defaults_only(tmp_path):
    """Only defaults file present → those values used."""
    defaults = tmp_path / "score_weights.yaml"
    defaults.write_text("lunch_overlap: 7\nday_edge_late: 2\n")
    w = fb.ScoreWeights.load(defaults, tmp_path / "missing.local.yaml")
    assert w.lunch_overlap == 7
    assert w.day_edge_late == 2
    # Unspecified fields stay at dataclass defaults
    assert w.day_edge_early == 5
    assert w.attendee_tz_outside_hours == 20


def test_score_weights_local_overrides_defaults(tmp_path):
    """Local file overlays defaults; only specified keys are overridden."""
    defaults = tmp_path / "score_weights.yaml"
    defaults.write_text(
        "conflict_movability_multiplier: 5\n"
        "lunch_overlap: 10\n"
        "attendee_tz_outside_hours: 20\n"
    )
    local = tmp_path / "score_weights.local.yaml"
    local.write_text("lunch_overlap: 1\n")  # only override one knob
    w = fb.ScoreWeights.load(defaults, local)
    assert w.lunch_overlap == 1                    # overridden
    assert w.conflict_movability_multiplier == 5   # inherited from defaults
    assert w.attendee_tz_outside_hours == 20       # inherited from defaults


def test_score_weights_unknown_key_warns_but_loads(tmp_path, capsys):
    """A typo'd key is ignored with a stderr warning, rest of the file still loads."""
    defaults = tmp_path / "score_weights.yaml"
    defaults.write_text("lunch_overlap: 7\ntypo_field: 999\n")
    w = fb.ScoreWeights.load(defaults, tmp_path / "missing.yaml")
    assert w.lunch_overlap == 7
    err = capsys.readouterr().err
    assert "unknown weight key" in err and "typo_field" in err


def test_score_slot_uses_custom_weight_for_lunch():
    """Pass weights with lunch_overlap=2 and verify the breakdown uses it."""
    start = _dt("2026-05-20T12:30:00-07:00")  # spans lunch
    end = _dt("2026-05-20T13:30:00-07:00")
    w = fb.ScoreWeights(lunch_overlap=2)
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17), weights=w)
    assert result["score"] == 98  # 100 - 2
    lunch = next(b for b in result["breakdown"] if "lunch" in b["label"])
    assert lunch["delta"] == -2


def test_score_slot_uses_custom_conflict_multiplier():
    """Higher conflict multiplier scales the per-conflict penalty proportionally."""
    conflicts = [{"attendee": "alice@x", "conflict": {"movability": 8, "category": "one_on_one"}}]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    # multiplier 10 means (10-8)*10 = 20 penalty (vs default 5*2 = 10)
    w = fb.ScoreWeights(conflict_movability_multiplier=10)
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17), weights=w)
    assert result["score"] == 80


def test_score_slot_uses_custom_tz_penalty():
    """A user who treats TZ-mismatch as a soft signal can dial it down."""
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    w = fb.ScoreWeights(attendee_tz_outside_hours=5)
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["bob@x"], attendee_timezones={"bob@x": "Europe/Berlin"},
        weights=w,
    )
    assert result["score"] == 95  # 100 - 5


# ---------------------------------------------------------------------------
# Attendee timezone exceptions (travel / temporary overrides)
# ---------------------------------------------------------------------------

def test_load_tz_exceptions_missing_file(tmp_path):
    """No file → empty list."""
    assert fb.load_attendee_timezone_exceptions(tmp_path / "no.yaml") == []


def test_load_tz_exceptions_parses_yaml_dates(tmp_path):
    """PyYAML parses ISO-formatted dates natively to dt.date; helper preserves."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "attendee_timezone_exceptions:\n"
        "  - email: Mike@example.com\n"
        "    tz: America/New_York\n"
        "    start: 2026-05-13\n"
        "    end:   2026-05-16\n"
        "    note:  NYC travel\n"
    )
    out = fb.load_attendee_timezone_exceptions(cfg)
    assert len(out) == 1
    ex = out[0]
    assert ex["email"] == "mike@example.com"  # lowercased
    assert ex["tz"] == "America/New_York"
    assert ex["start"] == dt.date(2026, 5, 13)
    assert ex["end"] == dt.date(2026, 5, 16)
    assert ex["note"] == "NYC travel"


def test_load_tz_exceptions_parses_string_dates(tmp_path):
    """Dates quoted as strings still parse via dt.date.fromisoformat."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "attendee_timezone_exceptions:\n"
        "  - email: alice@x\n"
        "    tz: Europe/Berlin\n"
        '    start: "2026-06-01"\n'
        '    end:   "2026-06-10"\n'
    )
    out = fb.load_attendee_timezone_exceptions(cfg)
    assert out[0]["start"] == dt.date(2026, 6, 1)
    assert out[0]["end"] == dt.date(2026, 6, 10)
    assert out[0]["note"] is None  # no note field


def test_load_tz_exceptions_skips_malformed(tmp_path, capsys):
    """Missing required fields → entry skipped with warning, others still load."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "attendee_timezone_exceptions:\n"
        "  - email: alice@x\n"
        "    tz: Europe/Berlin\n"
        # missing start + end
        "  - email: bob@x\n"
        "    tz: Asia/Tokyo\n"
        "    start: 2026-07-01\n"
        "    end:   2026-07-07\n"
    )
    out = fb.load_attendee_timezone_exceptions(cfg)
    err = capsys.readouterr().err
    assert "malformed tz exception" in err
    assert len(out) == 1
    assert out[0]["email"] == "bob@x"


# _effective_tz_for ---------------------------------------------------------

def test_effective_tz_returns_base_when_no_exception_matches():
    base = {"alice@x": "America/New_York"}
    exceptions = [{"email": "alice@x", "tz": "Europe/Berlin",
                   "start": dt.date(2026, 6, 1), "end": dt.date(2026, 6, 10), "note": "Berlin"}]
    tz, note = fb._effective_tz_for("alice@x", dt.date(2026, 5, 20), base, exceptions)
    assert tz == "America/New_York"
    assert note is None


def test_effective_tz_returns_exception_inside_window():
    base = {"alice@x": "America/New_York"}
    exceptions = [{"email": "alice@x", "tz": "Europe/Berlin",
                   "start": dt.date(2026, 6, 1), "end": dt.date(2026, 6, 10), "note": "Berlin"}]
    tz, note = fb._effective_tz_for("alice@x", dt.date(2026, 6, 5), base, exceptions)
    assert tz == "Europe/Berlin"
    assert note == "Berlin"


def test_effective_tz_endpoints_are_inclusive():
    base = {}
    exceptions = [{"email": "x@y", "tz": "Asia/Tokyo",
                   "start": dt.date(2026, 7, 1), "end": dt.date(2026, 7, 7), "note": None}]
    # Both endpoints match
    assert fb._effective_tz_for("x@y", dt.date(2026, 7, 1), base, exceptions)[0] == "Asia/Tokyo"
    assert fb._effective_tz_for("x@y", dt.date(2026, 7, 7), base, exceptions)[0] == "Asia/Tokyo"
    # Day outside window does not
    assert fb._effective_tz_for("x@y", dt.date(2026, 7, 8), base, exceptions)[0] is None


def test_effective_tz_first_match_wins_on_overlap():
    """If exceptions overlap, the first-declared entry wins. User can reorder
    if they need different precedence."""
    exceptions = [
        {"email": "x@y", "tz": "America/New_York",
         "start": dt.date(2026, 5, 1), "end": dt.date(2026, 5, 31), "note": "NYC May"},
        {"email": "x@y", "tz": "Europe/Berlin",
         "start": dt.date(2026, 5, 15), "end": dt.date(2026, 5, 20), "note": "Berlin overlap"},
    ]
    tz, note = fb._effective_tz_for("x@y", dt.date(2026, 5, 18), {}, exceptions)
    assert tz == "America/New_York"
    assert note == "NYC May"


def test_effective_tz_case_insensitive_email():
    base = {}
    exceptions = [{"email": "alice@x", "tz": "Asia/Tokyo",
                   "start": dt.date(2026, 7, 1), "end": dt.date(2026, 7, 7), "note": None}]
    tz, _ = fb._effective_tz_for("Alice@X", dt.date(2026, 7, 3), base, exceptions)
    assert tz == "Asia/Tokyo"


# score_slot integration with exceptions ------------------------------------

def test_score_slot_uses_base_tz_when_date_outside_exception_window():
    """Outside the Berlin window, carol is treated as NY (his base TZ).
    Slot at 10am PT = 1pm NY → inside hours → no penalty."""
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    base = {"carol@example.com": "America/New_York"}
    exceptions = [{"email": "carol@example.com", "tz": "Europe/Berlin",
                   "start": dt.date(2026, 6, 1), "end": dt.date(2026, 6, 10), "note": "Berlin"}]
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["carol@example.com"],
        attendee_timezones=base,
        attendee_timezone_exceptions=exceptions,
    )
    assert result["score"] == 100


def test_score_slot_uses_exception_tz_when_date_inside_window():
    """Inside the Berlin window, carol is Berlin. Slot at 10am PT = 7pm Berlin
    → outside hours → penalty fires, label includes note."""
    start = _dt("2026-06-05T10:00:00-07:00")  # Fri inside Berlin window
    end = _dt("2026-06-05T11:00:00-07:00")
    base = {"carol@example.com": "America/New_York"}
    exceptions = [{"email": "carol@example.com", "tz": "Europe/Berlin",
                   "start": dt.date(2026, 6, 1), "end": dt.date(2026, 6, 10), "note": "Berlin offsite"}]
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["carol@example.com"],
        attendee_timezones=base,
        attendee_timezone_exceptions=exceptions,
    )
    assert result["score"] == 80
    tz_entries = [b for b in result["breakdown"] if "outside working hours" in b["label"]]
    assert len(tz_entries) == 1
    label = tz_entries[0]["label"]
    assert "Europe/Berlin" in label
    assert "Berlin offsite" in label  # note included


def test_score_slot_exception_without_note_omits_dangling_comma():
    """When note is None, the label should not have a trailing ', None' or
    ', '. Visual cleanliness matters when Claude quotes the label."""
    start = _dt("2026-06-05T10:00:00-07:00")
    end = _dt("2026-06-05T11:00:00-07:00")
    exceptions = [{"email": "x@y", "tz": "Europe/Berlin",
                   "start": dt.date(2026, 6, 1), "end": dt.date(2026, 6, 10), "note": None}]
    result = fb.score_slot(
        start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17),
        attendees=["x@y"],
        attendee_timezones={},
        attendee_timezone_exceptions=exceptions,
    )
    label = next(b["label"] for b in result["breakdown"] if "outside working hours" in b["label"])
    assert ", None" not in label
    assert label.endswith(")")
    assert ", )" not in label


# ---------------------------------------------------------------------------
# Recurring frequency_in_window (task #9 — recurring/one-off signal refinement)
# ---------------------------------------------------------------------------

def test_classify_one_off_event_has_frequency_one():
    """A non-recurring event always reports frequency_in_window=1."""
    cls = fb.classify_event(_ev(summary="Customer call"))
    assert cls["recurring"] is False
    assert cls["recurring_event_id"] is None
    assert cls["frequency_in_window"] == 1


def test_classify_recurring_event_without_counts_defaults_to_one():
    """Recurring event but no `series_counts` supplied → defaults to 1 (we
    just don't know the cadence from this event alone)."""
    cls = fb.classify_event(_ev(summary="Weekly sync", recurring=True))
    assert cls["recurring"] is True
    assert cls["recurring_event_id"] == "rec123"
    assert cls["frequency_in_window"] == 1


def test_classify_uses_series_counts_when_provided():
    """When build_busy_map's first pass tells us a series appears N times in
    the window, that count flows through to the classification."""
    cls = fb.classify_event(
        _ev(summary="Weekly sync", recurring=True, recurring_event_id="rec-abc"),
        series_counts={"rec-abc": 4},
    )
    assert cls["frequency_in_window"] == 4


def test_classify_series_counts_only_apply_to_matching_id():
    """A series_counts entry for a different ID is ignored — each event uses
    its own recurringEventId for the lookup."""
    cls = fb.classify_event(
        _ev(summary="My sync", recurring=True, recurring_event_id="rec-mine"),
        series_counts={"rec-someone-else": 99},
    )
    assert cls["frequency_in_window"] == 1


def test_build_busy_map_counts_series_instances_per_attendee():
    """End-to-end through build_busy_map: a recurring weekly sync that
    appears 3 times in the window gets frequency_in_window=3; a separate
    one-off keeps 1."""
    # Three instances of the same weekly series + one one-off
    weekly_a = _ev(summary="Weekly", start="2026-05-20T10:00:00-07:00",
                   end="2026-05-20T10:30:00-07:00", recurring=True,
                   recurring_event_id="rec-weekly")
    weekly_b = _ev(summary="Weekly", start="2026-05-27T10:00:00-07:00",
                   end="2026-05-27T10:30:00-07:00", recurring=True,
                   recurring_event_id="rec-weekly")
    weekly_c = _ev(summary="Weekly", start="2026-06-03T10:00:00-07:00",
                   end="2026-06-03T10:30:00-07:00", recurring=True,
                   recurring_event_id="rec-weekly")
    one_off = _ev(summary="One-off chat", start="2026-05-21T14:00:00-07:00",
                  end="2026-05-21T15:00:00-07:00")
    busy = fb.build_busy_map({"alice@x": [weekly_a, weekly_b, weekly_c, one_off]})
    blocks = busy["alice@x"]
    assert len(blocks) == 4
    classifications_by_summary = {cls["summary"]: cls for _, _, cls in blocks}
    assert classifications_by_summary["Weekly"]["frequency_in_window"] == 3
    assert classifications_by_summary["One-off chat"]["frequency_in_window"] == 1


def test_build_busy_map_counts_are_per_attendee_not_global():
    """Two attendees attending different series with the same name should each
    get their own per-attendee count. (Different recurringEventIds.)"""
    alice_event = _ev(summary="Standup", recurring=True, recurring_event_id="rec-alice-standup",
                      start="2026-05-20T09:00:00-07:00", end="2026-05-20T09:15:00-07:00")
    bob_event_1 = _ev(summary="Standup", recurring=True, recurring_event_id="rec-bob-standup",
                      start="2026-05-20T09:00:00-07:00", end="2026-05-20T09:15:00-07:00")
    bob_event_2 = _ev(summary="Standup", recurring=True, recurring_event_id="rec-bob-standup",
                      start="2026-05-21T09:00:00-07:00", end="2026-05-21T09:15:00-07:00")
    busy = fb.build_busy_map({
        "alice@x": [alice_event],
        "bob@x":   [bob_event_1, bob_event_2],
    })
    assert busy["alice@x"][0][2]["frequency_in_window"] == 1
    assert busy["bob@x"][0][2]["frequency_in_window"] == 2


def test_score_slot_recurring_skippable_bonus_at_threshold():
    """Recurring conflict at exactly threshold frequency → bonus applies."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 8, "category": "one_on_one",
                     "recurring": True, "frequency_in_window": 3},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    # Base penalty (10-8)*5 = 10; bonus +5; net -5; score 95.
    assert result["score"] == 95
    bonuses = [b for b in result["breakdown"] if b["delta"] > 0]
    assert len(bonuses) == 1
    assert "3×" in bonuses[0]["label"]
    assert bonuses[0]["delta"] == 5


def test_score_slot_recurring_skippable_bonus_below_threshold():
    """Recurring conflict appearing fewer times than threshold → no bonus."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 8, "category": "one_on_one",
                     "recurring": True, "frequency_in_window": 2},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    # Base penalty -10; no bonus; score 90.
    assert result["score"] == 90
    assert all(b["delta"] <= 0 for b in result["breakdown"])


def test_score_slot_no_bonus_for_non_recurring_even_at_high_freq():
    """A one-off marked frequency_in_window=999 (shouldn't happen but harmless)
    must not trigger the bonus since recurring=False."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 8, "category": "one_on_one",
                     "recurring": False, "frequency_in_window": 99},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    assert result["score"] == 90
    assert all(b["delta"] <= 0 for b in result["breakdown"])


def test_score_slot_bonus_applies_per_conflict():
    """Two qualifying conflicts → bonus added twice."""
    conflicts = [
        {"attendee": "alice@x",
         "conflict": {"movability": 8, "category": "one_on_one",
                      "recurring": True, "frequency_in_window": 4}},
        {"attendee": "bob@x",
         "conflict": {"movability": 5, "category": "team_meeting",
                      "recurring": True, "frequency_in_window": 5}},
    ]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    # alice: -10 + 5 = -5
    # bob:   -25 + 5 = -20
    # net: 100 - 5 - 20 = 75
    assert result["score"] == 75
    bonuses = [b for b in result["breakdown"] if b["delta"] > 0]
    assert len(bonuses) == 2


# ---------------------------------------------------------------------------
# Calendar pattern memory: fingerprint, outcomes.jsonl, learned adjustment
# ---------------------------------------------------------------------------

def test_event_fingerprint_recurring_uses_event_id():
    """Recurring events are keyed by their recurringEventId — stable across
    instances regardless of title rename or attendee shuffle."""
    fp = fb.event_fingerprint(_ev(summary="Weekly", recurring=True, recurring_event_id="abc123"))
    assert fp == "rec::abc123"


def test_event_fingerprint_one_off_uses_summary_and_sorted_attendees():
    """One-offs hash (summary, sorted attendee emails). Sort means attendee
    order doesn't change the fingerprint."""
    fp_a = fb.event_fingerprint({
        "summary": "Quick chat",
        "start": {"dateTime": "2026-05-20T10:00:00-07:00"},
        "end":   {"dateTime": "2026-05-20T11:00:00-07:00"},
        "attendees": [{"email": "alice@x"}, {"email": "bob@x"}],
    })
    fp_b = fb.event_fingerprint({
        "summary": "Quick chat",
        "start": {"dateTime": "2026-05-20T10:00:00-07:00"},
        "end":   {"dateTime": "2026-05-20T11:00:00-07:00"},
        "attendees": [{"email": "bob@x"}, {"email": "alice@x"}],  # reversed
    })
    assert fp_a == fp_b == "oneoff::Quick chat::alice@x,bob@x"


def test_event_fingerprint_one_off_attendees_lowercased():
    """Mixed-case emails fingerprint the same as lowercase — calendar API
    sometimes returns mixed-case for display."""
    fp = fb.event_fingerprint({
        "summary": "Sync",
        "start": {"dateTime": "2026-05-20T10:00:00-07:00"},
        "end":   {"dateTime": "2026-05-20T11:00:00-07:00"},
        "attendees": [{"email": "Alice@X"}],
    })
    assert fp == "oneoff::Sync::alice@x"


def test_classify_event_emits_fingerprint():
    """The classification dict carries the fingerprint so downstream code
    (score_slot, make_ask_context) can do outcome lookups."""
    cls = fb.classify_event(_ev(summary="Weekly", recurring=True, recurring_event_id="rec-xyz"))
    assert cls["fingerprint"] == "rec::rec-xyz"


def test_load_outcomes_missing_file(tmp_path):
    """No log file → empty aggregate."""
    assert fb.load_outcomes(tmp_path / "no.jsonl") == {}


def test_load_outcomes_aggregates_per_attendee_and_event(tmp_path):
    """Multiple records aggregate into counters keyed by (fingerprint, email)."""
    log = tmp_path / "outcomes.jsonl"
    log.write_text(
        '{"ts": "2026-05-01T10:00:00-07:00", "attendee": "alice@x", "outcome": "moved", "event_fingerprint": "rec::abc"}\n'
        '{"ts": "2026-05-08T10:00:00-07:00", "attendee": "alice@x", "outcome": "moved", "event_fingerprint": "rec::abc"}\n'
        '{"ts": "2026-05-15T10:00:00-07:00", "attendee": "alice@x", "outcome": "declined", "event_fingerprint": "rec::abc"}\n'
        '{"ts": "2026-05-01T10:00:00-07:00", "attendee": "bob@x", "outcome": "moved", "event_fingerprint": "rec::abc"}\n'
    )
    out = fb.load_outcomes(log)
    assert out[("rec::abc", "alice@x")] == {
        "moved": 2,
        "declined": 1,
        "last_ts": "2026-05-15T10:00:00-07:00",
        "last_outcome": "declined",
    }
    assert out[("rec::abc", "bob@x")] == {
        "moved": 1,
        "last_ts": "2026-05-01T10:00:00-07:00",
        "last_outcome": "moved",
    }


def test_load_outcomes_skips_malformed_lines(tmp_path, capsys):
    """A bad JSON line or a record missing required fields is skipped
    with a stderr note, not allowed to crash the load."""
    log = tmp_path / "outcomes.jsonl"
    log.write_text(
        '{"ts": "2026-05-01T10:00:00-07:00", "attendee": "alice@x", "outcome": "moved", "event_fingerprint": "rec::abc"}\n'
        'not json at all\n'
        '{"missing": "fields"}\n'
        '\n'
        '# a comment line\n'
        '{"ts": "2026-05-15T10:00:00-07:00", "attendee": "bob@x", "outcome": "moved", "event_fingerprint": "rec::abc"}\n'
    )
    out = fb.load_outcomes(log)
    assert len(out) == 2
    err = capsys.readouterr().err
    assert "malformed" in err


def test_score_slot_learned_bonus_for_repeated_moves():
    """A conflict against an event/attendee with prior 'moved' outcomes gets
    a positive score adjustment (defaults: 2 per move, capped at 8)."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting", "fingerprint": "rec::ABC",
                     "summary": "Platform sync", "recurring": True, "frequency_in_window": 1},
    }]
    outcomes = {("rec::ABC", "alice@x"): {"moved": 2, "last_outcome": "moved"}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           outcomes_aggregated=outcomes)
    # Base penalty (10-5)*5 = 25; learned bonus 2*2 = +4; net -21; score 79
    assert result["score"] == 79
    learned = [b for b in result["breakdown"] if b["label"].startswith("learned:")]
    assert len(learned) == 1
    assert learned[0]["delta"] == 4
    assert "moved:2" in learned[0]["label"]


def test_score_slot_learned_penalty_for_declined():
    """A conflict against a (fingerprint, attendee) with a 'declined' history
    gets a NEGATIVE learned adjustment — don't keep proposing what they've
    already shot down."""
    conflicts = [{
        "attendee": "bob@x",
        "conflict": {"movability": 8, "category": "one_on_one", "fingerprint": "rec::XYZ",
                     "summary": "Bob 1:1", "recurring": True, "frequency_in_window": 1},
    }]
    outcomes = {("rec::XYZ", "bob@x"): {"declined": 2, "last_outcome": "declined"}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           outcomes_aggregated=outcomes)
    # Base penalty (10-8)*5 = 10; learned penalty -2*3 = -6; net -16; score 84
    assert result["score"] == 84
    learned = [b for b in result["breakdown"] if b["label"].startswith("learned:")]
    assert learned[0]["delta"] == -6
    assert "declined:2" in learned[0]["label"]


def test_score_slot_learned_adjustment_clamped_to_max():
    """A pile of past moves can't shift the conflict beyond ±learned_max_adjustment."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 0, "category": "ooo", "fingerprint": "rec::ABC",
                     "summary": "OOO", "recurring": True, "frequency_in_window": 1},
    }]
    outcomes = {("rec::ABC", "alice@x"): {"moved": 99, "last_outcome": "moved"}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           outcomes_aggregated=outcomes)
    learned = [b for b in result["breakdown"] if b["label"].startswith("learned:")]
    # 99 moves × 2 = 198, but clamped to learned_max_adjustment=8
    assert learned[0]["delta"] == 8


def test_score_slot_no_learned_entry_no_adjustment():
    """A conflict with a fingerprint that has no matching outcomes → no
    learned breakdown line."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting", "fingerprint": "rec::NEW",
                     "summary": "Fresh meeting", "recurring": True, "frequency_in_window": 1},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           outcomes_aggregated={("rec::OTHER", "alice@x"): {"moved": 99}})
    assert all(not b["label"].startswith("learned:") for b in result["breakdown"])


def test_make_ask_context_surfaces_outcome_history():
    """make_ask_context attaches the (fingerprint, attendee) outcome counter
    onto the conflict dict so Claude can cite it in messages."""
    ev = _ev(summary="Weekly", recurring=True, recurring_event_id="rec-W")
    cls = fb.classify_event(ev)
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T10:30:00-07:00")
    outcomes = {("rec::rec-W", "alice@x"): {"moved": 3, "declined": 1, "last_outcome": "moved"}}
    ctx = fb.make_ask_context("alice@x", (start, end, cls), outcomes)
    assert ctx["conflict"]["outcome_history"] == {"moved": 3, "declined": 1, "last_outcome": "moved"}


# ---------------------------------------------------------------------------
# Seniority — attendee_emails on classification + score_slot penalty (task #10)
# ---------------------------------------------------------------------------

def test_classify_emits_attendee_emails():
    """Classification carries the list of conflict attendees (lowercased,
    deduped, sorted) so score_slot and Claude can look up seniority/etc."""
    cls = fb.classify_event(_ev(
        summary="Big sync",
        attendees=[
            {"email": "Alice@X"},
            {"email": "bob@x"},
            {"email": "alice@x"},  # dup
        ],
    ))
    assert cls["attendee_emails"] == ["alice@x", "bob@x"]


def test_make_ask_context_surfaces_conflict_attendees():
    """The per-conflict ask context exposes conflict_attendees so Claude can
    cite who else is on the meeting in ask-messages."""
    ev = _ev(summary="Sync", attendees=[{"email": "alice@x"}, {"email": "bob@x"}])
    cls = fb.classify_event(ev)
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T11:00:00-07:00")
    ctx = fb.make_ask_context("alice@x", (start, end, cls))
    assert ctx["conflict"]["conflict_attendees"] == ["alice@x", "bob@x"]


def test_score_slot_seniority_penalty_at_director():
    """Director (tier 3) on the conflict → penalty (3-2)*5 = -5."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting",
                     "conflict_attendees": ["alice@x", "director@x"]},
    }]
    sen = {"director@x": {"tier": 3}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           seniority_map=sen)
    # Base penalty (10-5)*5 = 25; seniority -5; net -30; score 70
    assert result["score"] == 70
    sen_entry = [b for b in result["breakdown"] if b["label"].startswith("senior attendee")]
    assert len(sen_entry) == 1
    assert "director@x" in sen_entry[0]["label"]
    assert "tier 3" in sen_entry[0]["label"]
    assert sen_entry[0]["delta"] == -5


def test_score_slot_seniority_uses_max_tier_among_attendees():
    """When the conflict has multiple ranked attendees, the most senior
    determines the penalty (it's that person whose presence makes moving
    hard)."""
    conflicts = [{
        "attendee": "ic@x",
        "conflict": {"movability": 5, "category": "team_meeting",
                     "conflict_attendees": ["ic@x", "director@x", "vp@x"]},
    }]
    sen = {"director@x": {"tier": 3}, "vp@x": {"tier": 4}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           seniority_map=sen)
    # max tier is 4 (VP), penalty (4-2)*5 = 10
    sen_entry = next(b for b in result["breakdown"] if "senior attendee" in b["label"])
    assert "vp@x" in sen_entry["label"]
    assert sen_entry["delta"] == -10


def test_score_slot_seniority_below_threshold_no_penalty():
    """A senior IC (tier 1) on the conflict is at/below threshold (default 2) → no penalty."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting",
                     "conflict_attendees": ["alice@x", "staff@x"]},
    }]
    sen = {"staff@x": {"tier": 1}}
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           seniority_map=sen)
    assert all("senior attendee" not in b["label"] for b in result["breakdown"])


def test_score_slot_seniority_clamped_at_max():
    """A C-level (tier 5) over the default threshold (2) gives (5-2)*5 = 15.
    Custom configuration could exceed seniority_max_penalty; the cap kicks in."""
    conflicts = [{
        "attendee": "ic@x",
        "conflict": {"movability": 5, "category": "team_meeting",
                     "conflict_attendees": ["ic@x", "cto@x"]},
    }]
    sen = {"cto@x": {"tier": 5}}
    # Use a huge per-tier value so the cap engages
    w = fb.ScoreWeights(seniority_penalty_per_tier_above=100, seniority_max_penalty=30)
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           seniority_map=sen, weights=w)
    sen_entry = next(b for b in result["breakdown"] if "senior attendee" in b["label"])
    assert sen_entry["delta"] == -30  # clamped, not -300


# ---------------------------------------------------------------------------
# Required vs optional attendees (task #12)
# ---------------------------------------------------------------------------

def test_score_slot_required_attendee_full_penalty():
    """When required_attendees=None or includes the conflict's attendee,
    the full base penalty applies — preserves prior behavior."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting"},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    # Without required_attendees → behaves as required (full penalty)
    no_required = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17))
    assert no_required["score"] == 75  # 100 - (10-5)*5 = 75

    # required_attendees explicitly includes alice → same result
    with_required = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                                  required_attendees={"alice@x"})
    assert with_required["score"] == 75


def test_score_slot_optional_attendee_reduced_penalty():
    """Conflict's attendee not in required_attendees → penalty scaled by
    optional_attendee_penalty_multiplier (default 0.3)."""
    conflicts = [{
        "attendee": "carol@x",
        "conflict": {"movability": 5, "category": "team_meeting"},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           required_attendees={"alice@x", "bob@x"})  # carol absent → optional
    # Base penalty 25, multiplier 0.3 → 8 (rounded)
    assert result["score"] == 92
    label = next(b["label"] for b in result["breakdown"] if "carol" in b["label"])
    assert "[optional]" in label


def test_score_slot_optional_lookup_is_case_insensitive():
    """Conflict attendee in mixed case still matches lowercased required set."""
    conflicts = [{
        "attendee": "Alice@X",
        "conflict": {"movability": 5, "category": "team_meeting"},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           required_attendees={"alice@x"})
    # alice IS required (case-insensitive match) → full penalty, no [optional] label
    label = next(b["label"] for b in result["breakdown"] if "Alice" in b["label"])
    assert "[optional]" not in label


def test_score_slot_optional_multiplier_zero_ignores_conflict():
    """Setting multiplier to 0 effectively zeroes out optional conflicts."""
    conflicts = [{
        "attendee": "carol@x",
        "conflict": {"movability": 5, "category": "team_meeting"},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    w = fb.ScoreWeights(optional_attendee_penalty_multiplier=0.0)
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           required_attendees={"alice@x"}, weights=w)
    # carol's conflict penalty = base * 0 = 0; score stays at 100
    assert result["score"] == 100


def test_score_slot_required_and_optional_mixed():
    """Mixed conflicts: required takes full, optional takes reduced."""
    conflicts = [
        {"attendee": "alice@x", "conflict": {"movability": 5, "category": "team_meeting"}},
        {"attendee": "carol@x", "conflict": {"movability": 5, "category": "team_meeting"}},
    ]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           required_attendees={"alice@x"})
    # alice: full -25; carol: -25 * 0.3 = -8; net -33; score 67
    assert result["score"] == 67


# ---------------------------------------------------------------------------
# Lead-time penalty (task #19)
# ---------------------------------------------------------------------------

def _slot_around(now_str, offset_minutes, duration_minutes=30):
    """Return (slot_start, slot_end, now) where slot_start = now + offset."""
    now = _dt(now_str)
    start = now + dt.timedelta(minutes=offset_minutes)
    end = start + dt.timedelta(minutes=duration_minutes)
    return start, end, now


def test_score_slot_no_lead_time_penalty_when_now_is_none():
    """now=None preserves prior behavior — no lead-time penalty path runs."""
    start = _dt("2026-05-15T10:00:00-07:00")
    end = _dt("2026-05-15T10:30:00-07:00")
    result = fb.score_slot(start, end, conflicts=[], work_start=dt.time(9), work_end=dt.time(17))
    assert all("lead time" not in b["label"] for b in result["breakdown"])


def test_score_slot_full_within_hour_penalty_when_slot_is_now():
    """Slot starting exactly at now → full within-hour penalty (default 40)."""
    start, end, now = _slot_around("2026-05-15T14:00:00-07:00", offset_minutes=0)
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -40


def test_score_slot_within_hour_penalty_interpolates():
    """Slot 30 min from now → interpolated between within_hour (40) and
    same_day (10): 40*0.5 + 10*0.5 = 25."""
    start, end, now = _slot_around("2026-05-15T14:00:00-07:00", offset_minutes=30)
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -25


def test_score_slot_at_sixty_minutes_uses_same_day_penalty():
    """Slot at exactly +60 min lands at the same-day floor (default 10)."""
    start, end, now = _slot_around("2026-05-15T14:00:00-07:00", offset_minutes=60)
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -10


def test_score_slot_later_same_day_flat_penalty():
    """Slot several hours later same day → flat same-day penalty."""
    start, end, now = _slot_around("2026-05-15T09:00:00-07:00", offset_minutes=180)  # noon
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -10
    assert "same-day" in lead["label"]


def test_score_slot_next_day_no_lead_time_penalty():
    """Slot tomorrow → no lead-time penalty fired."""
    now = _dt("2026-05-15T14:00:00-07:00")
    start = _dt("2026-05-18T10:00:00-07:00")  # next Monday
    end = _dt("2026-05-18T10:30:00-07:00")
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    assert all("lead time" not in b["label"] for b in result["breakdown"])


def test_score_slot_past_slot_gets_full_penalty():
    """Defensive: a slot already in the past (slipped through) takes the
    heaviest penalty so it bottoms out in the ranking."""
    now = _dt("2026-05-15T14:00:00-07:00")
    start = _dt("2026-05-15T13:30:00-07:00")  # 30 min before now
    end = _dt("2026-05-15T14:00:00-07:00")
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -40
    assert "past" in lead["label"]


def test_score_slot_lead_time_uses_custom_weights():
    """Custom within-hour/same-day knobs scale correspondingly."""
    start, end, now = _slot_around("2026-05-15T14:00:00-07:00", offset_minutes=0)
    w = fb.ScoreWeights(lead_time_within_hour_penalty=80, lead_time_same_day_penalty=20)
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now, weights=w)
    lead = next(b for b in result["breakdown"] if b["label"].startswith("lead time"))
    assert lead["delta"] == -80


def test_score_slot_lead_time_zero_disables_penalty():
    """Setting both knobs to 0 effectively disables the feature."""
    start, end, now = _slot_around("2026-05-15T14:00:00-07:00", offset_minutes=0)
    w = fb.ScoreWeights(lead_time_within_hour_penalty=0, lead_time_same_day_penalty=0)
    result = fb.score_slot(start, end, [], dt.time(9), dt.time(17), now=now, weights=w)
    assert all("lead time" not in b["label"] for b in result["breakdown"])


def test_score_weights_loads_float_multiplier(tmp_path):
    """ScoreWeights.load casts to the right type per field annotation."""
    defaults = tmp_path / "score_weights.yaml"
    defaults.write_text("optional_attendee_penalty_multiplier: 0.5\nlunch_overlap: 7\n")
    w = fb.ScoreWeights.load(defaults, tmp_path / "missing.yaml")
    assert w.optional_attendee_penalty_multiplier == 0.5
    assert w.lunch_overlap == 7


def test_score_slot_no_penalty_when_seniority_map_empty():
    """No seniority data → no senior-attendee breakdown entry regardless of
    who's on the conflict."""
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 5, "category": "team_meeting",
                     "conflict_attendees": ["alice@x", "cto@x"]},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17),
                           seniority_map={})
    assert all("senior attendee" not in b["label"] for b in result["breakdown"])


def test_make_ask_context_no_history_returns_none():
    """No matching outcomes → outcome_history is None (Claude knows to not cite)."""
    ev = _ev(summary="Weekly", recurring=True, recurring_event_id="rec-NEW")
    cls = fb.classify_event(ev)
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T10:30:00-07:00")
    ctx = fb.make_ask_context("alice@x", (start, end, cls), outcomes_aggregated={})
    assert ctx["conflict"]["outcome_history"] is None


def test_score_slot_custom_threshold_and_bonus():
    """Threshold and bonus are configurable via ScoreWeights."""
    w = fb.ScoreWeights(recurring_skippable_threshold=5, recurring_skippable_bonus=15)
    conflicts = [{
        "attendee": "alice@x",
        "conflict": {"movability": 8, "category": "one_on_one",
                     "recurring": True, "frequency_in_window": 4},
    }]
    start = _dt("2026-05-20T14:00:00-07:00")
    end = _dt("2026-05-20T15:00:00-07:00")
    # freq=4 below threshold=5 → no bonus → score 90
    assert fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17), weights=w)["score"] == 90

    # Bump freq to 5 → bonus kicks in. With custom bonus=15, raw score would
    # be 100 - 10 + 15 = 105, but we cap at 100 (a slot can't be better than
    # a baseline all-free one).
    conflicts[0]["conflict"]["frequency_in_window"] = 5
    result = fb.score_slot(start, end, conflicts, dt.time(9), dt.time(17), weights=w)
    assert result["score"] == 100
    bonuses = [b for b in result["breakdown"] if b["delta"] > 0]
    assert bonuses[0]["delta"] == 15


def test_make_ask_context_surfaces_frequency_and_id():
    """The ask-context Claude reads should include the new fields so it can
    write messages like 'Alice's weekly sync — appears 4 times in this window'."""
    cls = fb.classify_event(
        _ev(summary="Weekly", recurring=True, recurring_event_id="rec-w"),
        series_counts={"rec-w": 4},
    )
    start = _dt("2026-05-20T10:00:00-07:00")
    end = _dt("2026-05-20T10:30:00-07:00")
    ctx = fb.make_ask_context("alice@x", (start, end, cls))
    assert ctx["conflict"]["recurring"] is True
    assert ctx["conflict"]["recurring_event_id"] == "rec-w"
    assert ctx["conflict"]["frequency_in_window"] == 4
