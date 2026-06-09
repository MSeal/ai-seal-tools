"""Tests for skills/voice-review/auto_categorize.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "voice-review"
sys.path.insert(0, str(SKILL_DIR))

from auto_categorize import (  # noqa: E402
    _set_field,
    categorize_review_file,
    load_synthetic_for_exemplar,
)


def _write_proposal(dir_path: Path, name: str, exemplars: list[dict]) -> None:
    payload = {
        "review_status": "pending",
        "candidate_exemplars": exemplars,
    }
    (dir_path / name).write_text(yaml.safe_dump(payload))


def _review_block(ex_id: str, flags: list[tuple[str, str]], decision: str = "reject") -> str:
    """Build a review file exemplar block. `flags` is a list of (rule, snippet)
    tuples that get formatted as the scrub finding lines."""
    flag_lines = "\n".join(f"- flag: [{rule}] {snippet} — detail here" for rule, snippet in flags)
    flagged_suffix = " — FLAGGED" if flags else ""
    return f"""### 1. {ex_id}{flagged_suffix}

- pattern: 'test pattern'
- synthetic: 'test synthetic'
- when: 'test when'
{flag_lines}
- decision: {decision}    # accept | reject
- reason:             # ...
- reason_notes:       # ...
"""


def test_set_field_replaces_existing():
    body = """- decision: reject    # comment
- reason:             # comment
"""
    out = _set_field(body, "reason", "false_positive:other")
    assert "- reason: false_positive:other" in out
    assert "- reason:             # comment" not in out


def test_set_field_inserts_after_decision_when_missing():
    body = "- decision: accept    # comment\n- other: thing\n"
    out = _set_field(body, "substitute", "auto")
    # New line should appear right after decision
    lines = out.splitlines()
    decision_idx = next(i for i, ln in enumerate(lines) if ln.startswith("- decision:"))
    assert lines[decision_idx + 1] == "- substitute: auto"


def test_load_synthetic_for_exemplar_combines_fields(tmp_path: Path):
    _write_proposal(tmp_path, "prop_test.yaml", [
        {"id": "ex_aaa_001", "synthetic": "Hello world", "pattern": "P", "when_to_use": "W"}
    ])
    out = load_synthetic_for_exemplar("ex_aaa_001", tmp_path)
    assert "Hello world" in out
    assert "P" in out
    assert "W" in out


def test_load_synthetic_skips_non_pending_proposals(tmp_path: Path):
    archived = {
        "review_status": "accepted",
        "candidate_exemplars": [{"id": "ex_archived_001", "synthetic": "should not match"}],
    }
    (tmp_path / "prop_archived.yaml").write_text(yaml.safe_dump(archived))
    out = load_synthetic_for_exemplar("ex_archived_001", tmp_path)
    assert out == ""


def test_categorize_substitutes_name_leak(tmp_path: Path):
    """Flagged exemplar where the proper-noun is in a people-context should
    flip to accept + substitute: auto."""
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    _write_proposal(proposals_dir, "prop_x.yaml", [{
        "id": "ex_nameleak_001",
        "synthetic": "Attendees: Alice Bob Stefan",
        "pattern": "attendee list",
        "when_to_use": "use for meeting notes",
    }])

    review_file = tmp_path / "review.md"
    review_file.write_text(f"""# Review

## prop_x.yaml

{_review_block("ex_nameleak_001", [("leak:proper_noun", "'Stefan'")])}
""")

    n_sub, n_fp, n_unch = categorize_review_file(review_file, proposals_dir)
    assert n_sub == 1
    assert n_fp == 0

    body = review_file.read_text()
    assert "- decision: accept" in body
    assert "- substitute: auto" in body
    assert "true_positive:override" in body


def test_categorize_marks_common_noun_as_false_positive(tmp_path: Path):
    """Flagged exemplar with only common-noun flags (no people-context) flips
    to accept + reason: false_positive:section_label."""
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    _write_proposal(proposals_dir, "prop_y.yaml", [{
        "id": "ex_falsepos_001",
        "synthetic": "BILLING\n- Invoices\n- Payments",
        "pattern": "label list",
        "when_to_use": "use for reference",
    }])

    review_file = tmp_path / "review.md"
    review_file.write_text(f"""# Review

## prop_y.yaml

{_review_block("ex_falsepos_001", [("leak:proper_noun", "'Invoices'")])}
""")

    n_sub, n_fp, n_unch = categorize_review_file(review_file, proposals_dir)
    assert n_sub == 0
    assert n_fp == 1

    body = review_file.read_text()
    assert "- decision: accept" in body
    assert "false_positive:section_label" in body
    assert "- substitute: auto" not in body


def test_categorize_leaves_ngram_flags_alone(tmp_path: Path):
    """Non-proper-noun flags (ngram_overlap, etc.) are out of scope — reviewer
    must decide. Stay as reject."""
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    _write_proposal(proposals_dir, "prop_z.yaml", [{
        "id": "ex_ngram_001",
        "synthetic": "verbatim phrase from source",
        "pattern": "P",
        "when_to_use": "W",
    }])

    review_file = tmp_path / "review.md"
    review_file.write_text(f"""# Review

## prop_z.yaml

{_review_block("ex_ngram_001", [("leak:ngram_overlap", "'phrase here'")])}
""")

    n_sub, n_fp, n_unch = categorize_review_file(review_file, proposals_dir)
    assert n_sub == 0
    assert n_fp == 0
    assert n_unch == 1

    body = review_file.read_text()
    assert "- decision: reject" in body


def test_categorize_skips_already_accepted_exemplars(tmp_path: Path):
    """If the reviewer already marked something as accept, don't overwrite."""
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    _write_proposal(proposals_dir, "prop_a.yaml", [{
        "id": "ex_accepted_001",
        "synthetic": "Stefan said hello",
        "pattern": "P",
        "when_to_use": "W",
    }])

    review_file = tmp_path / "review.md"
    review_file.write_text(f"""# Review

## prop_a.yaml

{_review_block("ex_accepted_001", [("leak:proper_noun", "'Stefan'")], decision="accept")}
""")

    n_sub, n_fp, n_unch = categorize_review_file(review_file, proposals_dir)
    assert n_sub == 0
    assert n_fp == 0

    body = review_file.read_text()
    # Original "- reason:             # ..." line shouldn't have been touched
    assert "true_positive:override" not in body
    assert "- substitute: auto" not in body
