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
        "  Alice@example.com: America/New_York\n"
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
        attendees=["Bob@example.com"],  # passed in mixed case
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
