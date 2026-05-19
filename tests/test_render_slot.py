"""Tests for render_slot — the markdown+ASCII timeline renderer for
freebusy slot dicts. Pure-function coverage of tick math, glyph
selection, annotation formatting, and full-card layout including the
>6-conflict fallback path."""

from __future__ import annotations

import datetime as dt

import pytest

import render_slot as rs


def _dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _conflict(
    *,
    start: str,
    end: str,
    summary: str = "Meeting",
    movability: int = 5,
    visible: bool = True,
) -> dict:
    return {
        "conflict_start": start,
        "conflict_end": end,
        "summary": summary,
        "movability": movability,
        "visible": visible,
    }


# ---------------------------------------------------------------------------
# glyph_for_movability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "movability,expected",
    [
        (10, rs.GLYPH_EASY),
        (7, rs.GLYPH_EASY),
        (6, rs.GLYPH_MODERATE),
        (4, rs.GLYPH_MODERATE),
        (3, rs.GLYPH_FIXED),
        (0, rs.GLYPH_FIXED),
    ],
)
def test_glyph_for_movability_visible(movability, expected):
    assert rs.glyph_for_movability(movability, visible=True) == expected


def test_glyph_for_movability_opaque_overrides_movability():
    """Opaque means we don't know the true severity — render '?'
    regardless of the movability field's value."""
    assert rs.glyph_for_movability(10, visible=False) == rs.GLYPH_OPAQUE
    assert rs.glyph_for_movability(0, visible=False) == rs.GLYPH_OPAQUE


# ---------------------------------------------------------------------------
# compute_boundaries
# ---------------------------------------------------------------------------


def test_boundaries_45_min_slot_with_15_min_ticks():
    """45-min slot → 3 intervals → 4 boundary points."""
    bs = rs.compute_boundaries(_dt("2026-05-22T14:30:00-07:00"), _dt("2026-05-22T15:15:00-07:00"))
    assert [b.strftime("%H:%M") for b in bs] == ["14:30", "14:45", "15:00", "15:15"]


def test_boundaries_30_min_slot():
    """30-min slot → 2 intervals → 3 boundaries."""
    bs = rs.compute_boundaries(_dt("2026-05-22T09:00:00-07:00"), _dt("2026-05-22T09:30:00-07:00"))
    assert len(bs) == 3
    assert bs[0].strftime("%H:%M") == "09:00"
    assert bs[-1].strftime("%H:%M") == "09:30"


def test_boundaries_60_min_slot():
    """60-min slot → 4 intervals → 5 boundaries."""
    bs = rs.compute_boundaries(_dt("2026-05-22T10:00:00-07:00"), _dt("2026-05-22T11:00:00-07:00"))
    assert len(bs) == 5


def test_boundaries_non_multiple_duration_clips_last_boundary():
    """If duration doesn't divide evenly by tick_minutes, round up and
    clip the trailing boundary to the actual slot end — the label
    shouldn't overshoot."""
    bs = rs.compute_boundaries(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T15:10:00-07:00"),  # 40-min slot
        tick_minutes=15,
    )
    assert bs[-1].strftime("%H:%M") == "15:10"  # clipped, not 15:15


# ---------------------------------------------------------------------------
# conflicts_overlapping
# ---------------------------------------------------------------------------


def test_overlap_full_coverage():
    c = _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00")
    result = rs.conflicts_overlapping(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [c],
    )
    assert result == [c]


def test_overlap_partial_at_start():
    """A 5-min overlap at the start still counts the whole tick."""
    c = _conflict(start="2026-05-22T14:25:00-07:00", end="2026-05-22T14:35:00-07:00")
    result = rs.conflicts_overlapping(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [c],
    )
    assert result == [c]


def test_overlap_partial_at_end():
    c = _conflict(start="2026-05-22T14:40:00-07:00", end="2026-05-22T14:50:00-07:00")
    result = rs.conflicts_overlapping(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [c],
    )
    assert result == [c]


def test_overlap_no_intersection():
    c = _conflict(start="2026-05-22T14:00:00-07:00", end="2026-05-22T14:15:00-07:00")
    result = rs.conflicts_overlapping(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [c],
    )
    assert result == []


def test_overlap_adjacent_does_not_count():
    """A conflict ending exactly at tick_start doesn't overlap (half-open
    interval). Same for a conflict starting at tick_end."""
    c1 = _conflict(start="2026-05-22T14:15:00-07:00", end="2026-05-22T14:30:00-07:00")
    c2 = _conflict(start="2026-05-22T14:45:00-07:00", end="2026-05-22T15:00:00-07:00")
    result = rs.conflicts_overlapping(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [c1, c2],
    )
    assert result == []


# ---------------------------------------------------------------------------
# tick_glyph
# ---------------------------------------------------------------------------


def test_tick_glyph_no_conflicts_is_free():
    assert rs.tick_glyph(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [],
    ) == rs.GLYPH_FREE


def test_tick_glyph_multi_conflict_picks_worst():
    """When two conflicts overlap the same tick, the lower-movability
    one wins (more visually alarming)."""
    movable = _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", movability=9)
    fixed = _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", movability=2)
    g = rs.tick_glyph(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [movable, fixed],
    )
    assert g == rs.GLYPH_FIXED


def test_tick_glyph_opaque_beats_visible():
    """When mixed with a visible conflict, the opaque one wins so we
    don't lie about severity we don't know."""
    movable = _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", movability=9, visible=True)
    op = _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", visible=False)
    g = rs.tick_glyph(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T14:45:00-07:00"),
        [movable, op],
    )
    assert g == rs.GLYPH_OPAQUE


# ---------------------------------------------------------------------------
# row_label
# ---------------------------------------------------------------------------


def test_row_label_marks_requester():
    label = rs.row_label("mseal@confluent.io", "mseal@confluent.io")
    assert label.strip() == "mseal (you)"


def test_row_label_case_insensitive_match():
    label = rs.row_label("MSeal@Confluent.io", "mseal@confluent.io")
    assert label.strip() == "MSeal (you)"


def test_row_label_non_requester_just_local_part():
    label = rs.row_label("alice@example.com", "mseal@confluent.io")
    assert label.strip() == "alice"


def test_row_label_truncates_overlong_emails():
    """Keep the column width fixed even for unusually long local parts."""
    label = rs.row_label("very-long-local-part@example.com", None, width=12)
    assert len(label) == 12
    assert label.endswith("…")


def test_row_label_padded_to_width():
    label = rs.row_label("a@x", "mseal@confluent.io", width=10)
    assert len(label) == 10


def test_row_label_uses_display_name_when_provided():
    """The full name (from Glean / passed via --names) replaces the
    email's local part. Way more scannable for multi-attendee reviews."""
    label = rs.row_label(
        "carol@example.com",
        None,
        names={"carol@example.com": "Carol Example"},
        width=20,
    )
    assert label.strip() == "Carol Example"


def test_row_label_display_name_with_requester_suffix():
    label = rs.row_label(
        "mseal@confluent.io",
        "mseal@confluent.io",
        names={"mseal@confluent.io": "Matthew Seal"},
        width=20,
    )
    assert label.strip() == "Matthew Seal (you)"


def test_row_label_falls_back_to_local_part_for_unmapped_email():
    """Mixed mapping (some have names, some don't) — unmapped emails
    still render rather than going blank."""
    label = rs.row_label(
        "unknown@example.com",
        None,
        names={"carol@example.com": "Carol Example"},
        width=18,
    )
    assert label.strip() == "unknown"


def test_compute_row_label_width_picks_longest():
    """Width adjusts so the longest label fits without truncation."""
    width = rs._compute_row_label_width(
        ["a@x", "carol@example.com"],
        "a@x",
        {"carol@example.com": "Carol Example"},
    )
    # "Carol Example" is 16 chars; min-width floor is higher, so 16 is fine.
    assert width >= 16


def test_compute_row_label_width_respects_min_floor():
    """For short attendee sets (single-letter names), width never goes
    below ROW_LABEL_MIN_WIDTH so the header still reads as a column."""
    width = rs._compute_row_label_width(["a@x"], None, {"a@x": "X"})
    assert width == rs.ROW_LABEL_MIN_WIDTH


def test_compute_row_label_width_caps_at_max():
    """For pathologically long names, width caps at ROW_LABEL_MAX_WIDTH;
    the row_label call then truncates with an ellipsis."""
    width = rs._compute_row_label_width(
        ["a@x"],
        None,
        {"a@x": "A" * 50},
    )
    assert width == rs.ROW_LABEL_MAX_WIDTH


# ---------------------------------------------------------------------------
# display_name_for / parse_names_arg
# ---------------------------------------------------------------------------


def test_display_name_for_basic():
    assert rs.display_name_for("carol@example.com", {"carol@example.com": "Carol Example"}) == "Carol Example"


def test_display_name_for_case_insensitive():
    assert rs.display_name_for("carol@example.com", {"carol@example.com": "Carol Example"}) == "Carol Example"


def test_display_name_for_missing_falls_back():
    assert rs.display_name_for("unknown@example.com", {"mseal@confluent.io": "Matthew"}) == "unknown"


def test_display_name_for_no_mapping():
    assert rs.display_name_for("mseal@confluent.io", None) == "mseal"
    assert rs.display_name_for("mseal@confluent.io", {}) == "mseal"


def test_parse_names_arg_basic():
    out = rs.parse_names_arg("mseal@confluent.io=Matthew Seal,alice@example.com=Alice Example")
    assert out == {
        "mseal@confluent.io": "Matthew Seal",
        "alice@example.com": "Alice Example",
    }


def test_parse_names_arg_empty():
    assert rs.parse_names_arg("") == {}


def test_parse_names_arg_lowercases_email_keys():
    """So lookup is uniform regardless of how the caller cased the email."""
    out = rs.parse_names_arg("MSeal@Confluent.io=Matthew Seal")
    assert "mseal@confluent.io" in out


def test_parse_names_arg_drops_malformed_entries():
    """Entries without '=' or with empty side are silently dropped — a
    bad mapping shouldn't kill the whole render."""
    out = rs.parse_names_arg("mseal=Matthew,no-equals,=NoEmail,alice=alice")
    assert out == {"mseal": "Matthew", "alice": "alice"}


# ---------------------------------------------------------------------------
# conflict_annotation
# ---------------------------------------------------------------------------


def test_annotation_single_movable():
    cs = [_conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="1:1 with Alex", movability=8)]
    assert rs.conflict_annotation(cs) == '"1:1 with Alex" (movability 8)'


def test_annotation_single_fixed_shows_warning():
    cs = [_conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="Customer call", movability=2)]
    assert rs.conflict_annotation(cs) == '"Customer call" (⚠ fixed; 2)'


def test_annotation_multiple_says_plus_n_more():
    cs = [
        _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="Worst", movability=2),
        _conflict(start="2026-05-22T14:45:00-07:00", end="2026-05-22T15:00:00-07:00", summary="Other", movability=8),
    ]
    out = rs.conflict_annotation(cs)
    assert '"Worst"' in out  # worst wins headline
    assert "+1 more" in out


def test_annotation_all_opaque():
    cs = [
        _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", visible=False),
        _conflict(start="2026-05-22T14:45:00-07:00", end="2026-05-22T15:00:00-07:00", visible=False),
    ]
    out = rs.conflict_annotation(cs)
    assert "opaque" in out
    assert "2" in out


def test_annotation_requester_gets_self_marker():
    cs = [_conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="Alex 1:1", movability=8)]
    out = rs.conflict_annotation(cs, is_requester=True)
    assert out.endswith("← you")


# ---------------------------------------------------------------------------
# render_timeline
# ---------------------------------------------------------------------------


def test_timeline_all_free_renders_dashes():
    out = rs.render_timeline(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T15:15:00-07:00"),
        [
            ("mseal@confluent.io", []),
            ("alice@example.com", []),
        ],
        "mseal@confluent.io",
    )
    lines = out.splitlines()
    # Header has time labels.
    assert "2:30" in lines[0]
    assert "3:15" in lines[0]
    # Two body rows, all dashes, no annotation.
    assert lines[1].lstrip().startswith("mseal (you)")
    assert rs.GLYPH_FREE * rs.TICK_WIDTH in lines[1]
    assert "←" not in lines[1] and '"' not in lines[1]


def test_timeline_partial_conflict_glyph_spans_only_overlapping_ticks():
    """A conflict from 14:30-15:00 (first two ticks) leaves the 15:00-15:15
    tick free."""
    out = rs.render_timeline(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T15:15:00-07:00"),
        [
            ("mseal@confluent.io", [
                _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T15:00:00-07:00", summary="Alex 1:1", movability=8),
            ]),
        ],
        "mseal@confluent.io",
    )
    mseal_row = next(l for l in out.splitlines() if "mseal" in l)
    # Glyph order: easy, easy, free.
    assert mseal_row.count(rs.GLYPH_EASY * rs.TICK_WIDTH) == 2
    assert mseal_row.count(rs.GLYPH_FREE * rs.TICK_WIDTH) == 1


def test_timeline_annotation_appended_to_conflict_row():
    out = rs.render_timeline(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T15:15:00-07:00"),
        [
            ("alice@example.com", [
                _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="Eng sync", movability=5),
            ]),
        ],
        "mseal@confluent.io",
    )
    erick_row = next(l for l in out.splitlines() if "alice" in l)
    assert '"Eng sync"' in erick_row
    assert "moderate; 5" in erick_row


def test_timeline_requester_row_gets_self_marker_in_annotation():
    out = rs.render_timeline(
        _dt("2026-05-22T14:30:00-07:00"),
        _dt("2026-05-22T15:15:00-07:00"),
        [
            ("mseal@confluent.io", [
                _conflict(start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00", summary="Alex 1:1", movability=8),
            ]),
        ],
        "mseal@confluent.io",
    )
    mseal_row = next(l for l in out.splitlines() if "mseal" in l)
    assert "← you" in mseal_row


# ---------------------------------------------------------------------------
# format_slot_card
# ---------------------------------------------------------------------------


def _slot(
    *,
    start: str,
    end: str,
    score: int = 100,
    conflicts: list[dict] | None = None,
) -> dict:
    return {
        "start": start,
        "end": end,
        "score": score,
        "score_breakdown": [],
        "conflicts": conflicts or [],
    }


def test_slot_card_all_free_has_header_and_timeline():
    card = rs.format_slot_card(
        _slot(start="2026-05-22T14:30:00-07:00", end="2026-05-22T15:15:00-07:00"),
        attendees=["mseal@confluent.io", "alice@example.com"],
        requester_email="mseal@confluent.io",
        rank=1,
    )
    assert "1. **" in card
    assert "Score 100" in card
    assert "all free" in card
    assert "```" in card  # code block fence
    assert "mseal (you)" in card
    assert "alice" in card


def test_slot_card_conflict_routes_to_correct_attendee():
    """A conflict whose `attendee` field points at alice should show up
    on alice's row, not mseal's."""
    card = rs.format_slot_card(
        _slot(
            start="2026-05-22T14:30:00-07:00",
            end="2026-05-22T15:15:00-07:00",
            score=80,
            conflicts=[{
                "attendee": "alice@example.com",
                "conflict": _conflict(
                    start="2026-05-22T14:30:00-07:00",
                    end="2026-05-22T14:45:00-07:00",
                    summary="Eng sync",
                    movability=5,
                ),
            }],
        ),
        attendees=["mseal@confluent.io", "alice@example.com"],
        requester_email="mseal@confluent.io",
    )
    mseal_row = next(l for l in card.splitlines() if "mseal" in l)
    erick_row = next(l for l in card.splitlines() if "alice" in l and "mseal" not in l)
    assert '"Eng sync"' in erick_row
    assert '"Eng sync"' not in mseal_row


def test_slot_card_more_than_max_attendees_falls_back_to_list():
    """When >max conflicted attendees, suppress the timeline and use a
    flat list with the coordination-not-scheduling hint."""
    conflicts = []
    attendees = []
    for i in range(8):
        email = f"attendee{i}@x"
        attendees.append(email)
        conflicts.append({
            "attendee": email,
            "conflict": _conflict(
                start="2026-05-22T14:30:00-07:00",
                end="2026-05-22T14:45:00-07:00",
                summary=f"Conflict {i}",
                movability=5,
            ),
        })
    card = rs.format_slot_card(
        _slot(
            start="2026-05-22T14:30:00-07:00",
            end="2026-05-22T15:15:00-07:00",
            score=20,
            conflicts=conflicts,
        ),
        attendees=attendees,
        max_visible_attendees=6,
    )
    assert "too many to visualize" in card
    assert "coordination problem" in card
    # Each attendee gets a list entry.
    for i in range(8):
        assert f"attendee{i}" in card


def test_slot_card_summary_counts_moderate_separately():
    """Moderates (movability 4–6) weren't counted before — the summary
    said '2 conflicts — 1 fixed' for a fixed+moderate slot, dropping
    the moderate from the breakdown. Lock the fix in."""
    card = rs.format_slot_card(
        _slot(
            start="2026-05-22T14:30:00-07:00",
            end="2026-05-22T15:00:00-07:00",
            conflicts=[
                {"attendee": "a@x", "conflict": _conflict(
                    start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00",
                    summary="Fixed", movability=2,
                )},
                {"attendee": "b@x", "conflict": _conflict(
                    start="2026-05-22T14:30:00-07:00", end="2026-05-22T14:45:00-07:00",
                    summary="Moderate", movability=5,
                )},
            ],
        ),
        attendees=["a@x", "b@x"],
    )
    header = card.splitlines()[0]
    assert "1 fixed" in header
    assert "1 moderate" in header


def test_slot_card_summary_flags_fixed_conflicts_with_warning():
    card = rs.format_slot_card(
        _slot(
            start="2026-05-22T14:30:00-07:00",
            end="2026-05-22T15:15:00-07:00",
            score=40,
            conflicts=[{
                "attendee": "alice@x",
                "conflict": _conflict(
                    start="2026-05-22T14:30:00-07:00",
                    end="2026-05-22T14:45:00-07:00",
                    summary="Customer call",
                    movability=2,
                ),
            }],
        ),
        attendees=["mseal@confluent.io", "alice@x"],
    )
    # Look for the warning marker only in the slot's header line
    # (the annotation also contains ⚠ but that's in the body).
    header = card.splitlines()[0]
    assert "⚠" in header
    assert "1 fixed" in header


def test_slot_card_full_names_replace_email_handles_in_timeline():
    """End-to-end: passing names to format_slot_card swaps the email
    handle in each attendee row for the display name."""
    card = rs.format_slot_card(
        _slot(start="2026-05-22T14:30:00-07:00", end="2026-05-22T15:00:00-07:00"),
        attendees=["mseal@confluent.io", "carol@example.com"],
        requester_email="mseal@confluent.io",
        names={
            "mseal@confluent.io": "Matthew Seal",
            "carol@example.com": "Carol Example",
        },
    )
    assert "Matthew Seal (you)" in card
    assert "Carol Example" in card
    assert "mseal " not in card  # email handle shouldn't leak
    assert "carol " not in card


def test_slot_card_header_includes_date_and_duration():
    card = rs.format_slot_card(
        _slot(start="2026-05-22T14:30:00-07:00", end="2026-05-22T15:15:00-07:00"),
        attendees=["mseal@confluent.io"],
    )
    assert "Fri, May 22" in card
    assert "45 min" in card
