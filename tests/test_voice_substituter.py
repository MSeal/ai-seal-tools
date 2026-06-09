"""Tests for the substituter — replaces flagged names with placeholders
and specific numbers with generic patterns."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "voice-review"
sys.path.insert(0, str(SKILL_DIR))

from substituter import (  # noqa: E402
    in_people_context,
    should_substitute_name,
    substitute_text,
    substitute_exemplar,
)


# ---------- people-context detection ----------

def test_in_people_context_attendees_marker():
    text = "Attendees: Alice Bob the team lead Stefan"
    assert in_people_context(text, "Stefan")


def test_in_people_context_adjacent_placeholder_name():
    """Immediate adjacency is the job of in_strong_name_context, not
    in_people_context. The people-context check requires an EXPLICIT marker
    (Attendees:, By:, etc.) — same-line proximity isn't enough because
    bullet lists and label brackets often share lines with placeholders."""
    from substituter import in_strong_name_context
    text = "The reviewers were Alice Chen and Bob Marquez today."
    # Bob Marquez is immediately adjacent → strong context
    assert in_strong_name_context(text, "Marquez")
    assert in_strong_name_context(text, "Chen")
    # But people-context (broad) requires an explicit marker
    text_with_marker = "Reviewers: Bob Marquez and Carol Osei."
    assert in_people_context(text_with_marker, "Marquez")


def test_not_in_people_context_standalone_paragraph():
    text = "The new Backend API exposes a paginated endpoint for the consumer service."
    assert not in_people_context(text, "Backend")


def test_in_people_context_after_by_marker():
    text = "Owners: Alice. Co-author: Stefan Sprenger."
    assert in_people_context(text, "Stefan")
    assert in_people_context(text, "Sprenger")


# ---------- name-substitution decision ----------

def test_common_english_word_not_substituted():
    """Words in the lexicon are false positives, not name leaks."""
    text = "The Backend handles authentication. The Frontend handles rendering."
    assert not should_substitute_name(text, "Backend")
    assert not should_substitute_name(text, "Frontend")


def test_real_name_in_people_context_substituted():
    text = "Attendees: Alice Bob Stefan"
    assert should_substitute_name(text, "Stefan")


def test_domain_term_outside_people_context_not_substituted():
    """`TaskRunners` and `Federated` are not in the lexicon AND not in a
    people context — leave for human review (don't substitute)."""
    text = "The system tracks Jobs, Checkpoints, and TaskRunners."
    assert not should_substitute_name(text, "TaskRunners")
    assert not should_substitute_name(text, "Checkpoints")


# ---------- name substitution ----------

def test_substitute_standalone_name():
    text = "Attendees: Alice Bob the team lead Stefan"
    new_text, mapping = substitute_text(text, ["Stefan"])
    assert "Stefan" not in new_text
    assert mapping["Stefan"] in ("Alice", "Bob", "Carol", "Dave", "Eve", "Frank")


def test_substitute_first_last_name_pattern():
    """`Bob Marquez` → keeps Bob, replaces Marquez with a placeholder surname."""
    text = "The reviewers were Alice Chen, Bob Marquez, and Carol Osei."
    new_text, mapping = substitute_text(text, ["Chen", "Marquez", "Osei"])
    assert "Marquez" not in new_text
    assert "Osei" not in new_text
    assert "Chen" not in new_text
    assert "Alice" in new_text
    assert "Bob" in new_text
    assert "Carol" in new_text
    # Each lastname gets a placeholder surname
    for lastname in ["Chen", "Marquez", "Osei"]:
        assert mapping[lastname] in {"Example", "Sample", "Demo", "Test", "Placeholder", "Generic"}


def test_substitute_skips_common_english_words():
    """`Backend` is in lexicon — should NOT be substituted even if flagged."""
    text = "Backend service handles request routing."
    new_text, mapping = substitute_text(text, ["Backend"])
    assert "Backend" in new_text
    assert "Backend" not in mapping


def test_substitute_skips_words_outside_people_context():
    """`TaskRunners` not in lexicon AND not in people context — left alone."""
    text = "The system tracks Jobs, Checkpoints, and TaskRunners."
    new_text, mapping = substitute_text(text, ["TaskRunners", "Checkpoints"])
    assert "TaskRunners" in new_text
    assert "Checkpoints" in new_text


# ---------- number substitution ----------

def test_substitute_years():
    text = "Released in 2024. Next milestone is in 2025."
    new_text, _ = substitute_text(text, [])
    assert "<YYYY>" in new_text
    assert "2024" not in new_text
    assert "2025" not in new_text


def test_substitute_multi_digit_numbers():
    text = "34 Teams Onboarded, 11 In Pilot, 6 Awaiting Access."
    new_text, _ = substitute_text(text, [])
    assert "34" not in new_text
    assert "11" not in new_text
    # Single-digit "6" should remain
    assert "6" in new_text
    assert "<N>" in new_text


def test_preserves_single_digit_numbers():
    text = "Three priorities: 1, 2, 3."
    new_text, _ = substitute_text(text, [])
    assert "1, 2, 3" in new_text  # single digits preserved


def test_skip_numbers_in_compound_tokens():
    """`v2`, `K8s` style tokens shouldn't get number-substituted."""
    text = "Use v2 of the K8s operator."
    new_text, _ = substitute_text(text, [])
    assert "v2" in new_text
    assert "K8s" in new_text


def test_substitute_numbers_disabled_keeps_them():
    text = "Released in 2024."
    new_text, _ = substitute_text(text, [], substitute_numbers=False)
    assert "2024" in new_text


# ---------- end-to-end exemplar substitution ----------

def test_substitute_exemplar_real_name_leak():
    """An exemplar with a name leak in synthetic gets that field rewritten."""
    exemplar = {
        "id": "ex_xxxx_001",
        "pattern": "Attendee-list opener",
        "synthetic": "Attendees: Alice Bob Stefan\n\nMet on 14 March 2025 to discuss the deploy.",
        "when_to_use": "Use when listing attendees.",
        "scrub_status": "flagged",
        "scrub_findings": [
            {"rule": "leak:proper_noun", "snippet": "Stefan", "detail": "x", "where": "y"},
        ],
    }
    new_exemplar, mapping = substitute_exemplar(exemplar)
    assert "Stefan" not in new_exemplar["synthetic"]
    # Numbers replaced too
    assert "2025" not in new_exemplar["synthetic"]
    assert "14" not in new_exemplar["synthetic"]
    assert "Stefan" in mapping


def test_substitute_exemplar_false_positive_unchanged():
    """When the only flag is a common-English word, no substitution happens."""
    exemplar = {
        "id": "ex_xxxx_002",
        "pattern": "Section",
        "synthetic": "Backend service notes\n· Add retry logic\n· Improve error handling",
        "when_to_use": "Use when documenting backend changes.",
        "scrub_status": "flagged",
        "scrub_findings": [
            {"rule": "leak:proper_noun", "snippet": "Backend", "detail": "x", "where": "y"},
        ],
    }
    new_exemplar, mapping = substitute_exemplar(exemplar, substitute_numbers=False)
    assert new_exemplar["synthetic"] == exemplar["synthetic"]
    assert "Backend" not in mapping
