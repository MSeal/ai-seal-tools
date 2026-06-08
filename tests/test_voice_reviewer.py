"""Tests for the voice-review skill's merge logic.

We focus on the pure functions (apply_proposal_to_profile, merge_stats_block,
merge_descriptors, accept_exemplar). The interactive CLI is not tested here —
that's `cmd_walk` and is hand-tested.
"""
from __future__ import annotations

from pathlib import Path
import sys

# Make skills/voice-review importable
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "voice-review"
sys.path.insert(0, str(SKILL_DIR))

import pytest

import reviewer  # noqa: E402
from reviewer import (  # noqa: E402
    accept_exemplar,
    apply_proposal_to_profile,
    ensure_audience_bucket,
    merge_descriptors,
    merge_stats_block,
    update_sources_seen,
    weighted_merge_number,
)


def make_proposal(**overrides):
    base = {
        "schema_version": 1,
        "proposal_id": "prop_test_001",
        "created_at": "2026-05-25T10:00:00Z",
        "source_hash": "a" * 64,
        "source_word_count": 500,
        "classification": {
            "audience": "technical_peer",
            "audience_confidence": 0.9,
            "audience_alternates": [],
            "doc_type": "polished",
            "doc_type_confidence": 0.9,
            "axis_estimates": {
                "formality": 0.6, "technical_density": 0.8, "brevity": 0.4, "warmth": 0.3,
            },
            "reasoning": "test",
        },
        "proposed_stats": {
            "sentence_length": {"mean": 22.0, "median": 20.0},
            "hedge_per_100w": 2.5,
        },
        "proposed_descriptors": {
            "voice_summary": "Direct and pragmatic.",
            "rhetorical_moves": ["Names tradeoff before recommendation"],
            "tics": ["Uses 'effectively' before assessments"],
            "structural_habits": [],
            "openings_inventory": [],
            "closings_inventory": [],
            "transition_style": "Short pivots.",
            "humor_register": "none observed",
            "self_reference_behavior": "Uses we for team work.",
            "what_to_avoid": ["Avoid passive constructions"],
        },
        "candidate_exemplars": [
            {
                "id": "ex_aaaaa_001",
                "pattern_id": "trade",
                "pattern": "Tradeoff first",
                "synthetic": "Choice is X vs Y — X gives speed, Y stability.",
                "when_to_use": "Use when recommending alternatives.",
                "source_hash": "a" * 64,
                "scrub_status": "passed",
            },
            {
                "id": "ex_aaaaa_002",
                "pattern_id": "flag",
                "pattern": "Flagged pattern",
                "synthetic": "Some leaky thing.",
                "when_to_use": "When something leaks.",
                "source_hash": "a" * 64,
                "scrub_status": "flagged",
                "scrub_findings": [{"rule": "leak:proper_noun", "snippet": "Leaky", "detail": "x", "where": "exemplar:ex_aaaaa_002"}],
            },
        ],
        "scrub_findings": [],
        "review_status": "pending",
    }
    base.update(overrides)
    return base


def empty_profile():
    return {
        "schema_version": 1,
        "generated_at": "2026-05-01T00:00:00Z",
        "last_updated": "2026-05-01T00:00:00Z",
        "audience_registry": {
            "technical_peer": "Design docs",
            "leadership": "Status updates",
            "direct_report": "1:1 prep",
            "cross_functional": "Specs",
            "external_public": "Blog posts",
            "casual": "DMs",
            "self_notes": "Personal",
        },
        "audiences": {},
        "shared_anti_patterns": [],
        "merge_history": [],
    }


# ---------- weighted merge ----------

def test_weighted_merge_none_returns_new():
    assert weighted_merge_number(None, 10.0, old_count=0) == 10.0


def test_weighted_merge_first_value():
    """Old count 0, k=1 → new value entirely takes over."""
    assert weighted_merge_number(5.0, 10.0, old_count=0, k=1) == 10.0


def test_weighted_merge_second_value_half_each():
    """Old count 1, new k=1 → average of old + new."""
    assert weighted_merge_number(5.0, 10.0, old_count=1, k=1) == pytest.approx(7.5)


def test_weighted_merge_caps_old_count():
    """Old count > cap means new contributes 1/(cap+1) regardless of true age."""
    capped = weighted_merge_number(10.0, 0.0, old_count=1000, k=1, cap=10)
    # (10 * 10 + 1 * 0) / 11 = 9.0909...
    assert capped == pytest.approx(100/11)


def test_merge_stats_block_recursive():
    old = {"sentence_length": {"mean": 20.0}, "hedge_per_100w": 1.0}
    new = {"sentence_length": {"mean": 30.0}, "hedge_per_100w": 3.0}
    merged = merge_stats_block(old, new, old_count=1, k=1)
    assert merged["sentence_length"]["mean"] == pytest.approx(25.0)
    assert merged["hedge_per_100w"] == pytest.approx(2.0)


def test_merge_stats_block_new_keys_added():
    old = {"hedge_per_100w": 1.0}
    new = {"booster_per_100w": 0.5}
    merged = merge_stats_block(old, new, old_count=1, k=1)
    # Old key preserved, new key added
    assert merged["hedge_per_100w"] == 1.0
    assert merged["booster_per_100w"] == 0.5


def test_merge_stats_block_empty_old_returns_new():
    new = {"sentence_length": {"mean": 22.0}}
    merged = merge_stats_block({}, new, old_count=0, k=1)
    assert merged == new


# ---------- descriptor merge ----------

def test_merge_descriptors_list_appends_unique():
    old = {"rhetorical_moves": ["A", "B"]}
    new = {"rhetorical_moves": ["B", "C", "D"]}
    merged = merge_descriptors(old, new, accepted_fields={"rhetorical_moves"})
    assert merged["rhetorical_moves"] == ["A", "B", "C", "D"]


def test_merge_descriptors_prose_replaces():
    old = {"voice_summary": "Old summary"}
    new = {"voice_summary": "New summary"}
    merged = merge_descriptors(old, new, accepted_fields={"voice_summary"})
    assert merged["voice_summary"] == "New summary"


def test_merge_descriptors_only_accepted_fields_apply():
    old = {"voice_summary": "Old", "tics": ["X"]}
    new = {"voice_summary": "New", "tics": ["Y"]}
    merged = merge_descriptors(old, new, accepted_fields={"voice_summary"})
    assert merged["voice_summary"] == "New"
    assert merged["tics"] == ["X"]  # unchanged


def test_merge_descriptors_case_insensitive_dedup():
    """Don't add the same descriptor twice even if case differs."""
    old = {"tics": ["Uses em-dashes"]}
    new = {"tics": ["uses em-dashes"]}
    merged = merge_descriptors(old, new, accepted_fields={"tics"})
    assert len(merged["tics"]) == 1


# ---------- audience bucket ----------

def test_ensure_audience_bucket_creates_full_path():
    profile = empty_profile()
    axes = {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5}
    bucket = ensure_audience_bucket(profile, "technical_peer", "polished", axes)
    assert "technical_peer" in profile["audiences"]
    assert "polished" in profile["audiences"]["technical_peer"]["types"]
    assert bucket["sources_count"] == 0
    assert profile["audiences"]["technical_peer"]["axis_baseline"] == axes


def test_ensure_audience_bucket_idempotent():
    profile = empty_profile()
    axes = {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5}
    b1 = ensure_audience_bucket(profile, "technical_peer", "polished", axes)
    b1["sources_count"] = 5  # mutate
    b2 = ensure_audience_bucket(profile, "technical_peer", "polished", axes)
    # Same bucket — preserved sources_count
    assert b2["sources_count"] == 5


# ---------- accept_exemplar ----------

def test_accept_exemplar_strips_scrub_fields():
    """The accepted exemplar should only carry profile-shape fields."""
    cand = {
        "id": "ex_xyz_001",
        "pattern_id": "p",
        "pattern": "Foo",
        "synthetic": "Bar baz X versus Y.",
        "when_to_use": "Use when relevant.",
        "source_hash": "a" * 64,
        "scrub_status": "passed",
    }
    out = accept_exemplar(cand, source_hash="b" * 64)
    assert "scrub_status" not in out
    assert "pattern_id" not in out
    assert out["source_hash"] == "b" * 64
    assert "reviewed_at" in out


def test_accept_exemplar_adds_notes_when_provided():
    cand = {
        "id": "ex_xyz_001", "pattern_id": "p", "pattern": "Foo",
        "synthetic": "Bar.", "when_to_use": "When.",
        "source_hash": "a" * 64, "scrub_status": "passed",
    }
    out = accept_exemplar(cand, source_hash="a" * 64, reviewer_notes="manual override")
    assert out["reviewer_notes"] == "manual override"


# ---------- apply_proposal_to_profile end-to-end ----------

def test_apply_proposal_full_accept():
    profile = empty_profile()
    proposal = make_proposal()
    decisions = {
        "audience": "technical_peer",
        "doc_type": "polished",
        "merge_stats": True,
        "merge_descriptors": True,
        "accepted_descriptor_fields": set(proposal["proposed_descriptors"].keys()),
        "accepted_exemplar_ids": {"ex_aaaaa_001"},  # accept only the clean one
        "exemplar_notes": {},
    }
    new_profile = apply_proposal_to_profile(profile, proposal, decisions)

    aud = new_profile["audiences"]["technical_peer"]
    bucket = aud["types"]["polished"]

    assert aud["sources_count"] == 1
    assert bucket["sources_count"] == 1
    assert bucket["stats"]["hedge_per_100w"] == 2.5
    assert bucket["descriptors"]["voice_summary"] == "Direct and pragmatic."
    assert len(bucket["exemplars"]) == 1
    assert bucket["exemplars"][0]["id"] == "ex_aaaaa_001"
    # Flagged exemplar was NOT auto-included
    assert all(e["id"] != "ex_aaaaa_002" for e in bucket["exemplars"])

    # Merge history appended
    assert len(new_profile["merge_history"]) == 1
    assert "prop_test_001" in new_profile["merge_history"][0]["notes"]


def test_apply_proposal_stats_only():
    profile = empty_profile()
    proposal = make_proposal()
    decisions = {
        "audience": "technical_peer",
        "doc_type": "polished",
        "merge_stats": True,
        "merge_descriptors": False,
        "accepted_descriptor_fields": set(),
        "accepted_exemplar_ids": set(),
        "exemplar_notes": {},
    }
    new_profile = apply_proposal_to_profile(profile, proposal, decisions)
    bucket = new_profile["audiences"]["technical_peer"]["types"]["polished"]
    assert bucket["stats"]["hedge_per_100w"] == 2.5
    assert bucket["descriptors"] == {}
    assert bucket["exemplars"] == []


def test_apply_proposal_second_merge_averages_stats():
    profile = empty_profile()
    proposal1 = make_proposal(source_hash="a" * 64)
    proposal2 = make_proposal(
        source_hash="b" * 64,
        proposal_id="prop_test_002",
        proposed_stats={"sentence_length": {"mean": 40.0}, "hedge_per_100w": 0.5},
    )
    accept_all_fields = set(proposal1["proposed_descriptors"].keys())
    base_decisions = dict(
        audience="technical_peer",
        doc_type="polished",
        merge_stats=True,
        merge_descriptors=False,
        accepted_descriptor_fields=set(),
        accepted_exemplar_ids=set(),
        exemplar_notes={},
    )
    profile = apply_proposal_to_profile(profile, proposal1, base_decisions)
    profile = apply_proposal_to_profile(profile, proposal2, base_decisions)
    bucket = profile["audiences"]["technical_peer"]["types"]["polished"]
    # After first merge: hedge_per_100w = 2.5. After second with old_count=1, k=1:
    # (1*2.5 + 1*0.5)/2 = 1.5
    assert bucket["stats"]["hedge_per_100w"] == pytest.approx(1.5)
    assert bucket["sources_count"] == 2


def test_apply_proposal_routes_by_audience():
    profile = empty_profile()
    proposal = make_proposal()
    decisions = dict(
        audience="leadership",  # override the classifier
        doc_type="polished",
        merge_stats=True,
        merge_descriptors=False,
        accepted_descriptor_fields=set(),
        accepted_exemplar_ids=set(),
        exemplar_notes={},
    )
    new_profile = apply_proposal_to_profile(profile, proposal, decisions)
    # Routed to leadership, not technical_peer
    assert "leadership" in new_profile["audiences"]
    assert "technical_peer" not in new_profile["audiences"]


def test_apply_proposal_validates_against_schema():
    """End-to-end: result must be a valid profile v1."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "voice-analyze"))
    import schema as voice_schema

    profile = empty_profile()
    proposal = make_proposal()
    decisions = dict(
        audience="technical_peer",
        doc_type="polished",
        merge_stats=True,
        merge_descriptors=True,
        accepted_descriptor_fields=set(proposal["proposed_descriptors"].keys()),
        accepted_exemplar_ids={"ex_aaaaa_001"},
        exemplar_notes={},
    )
    new_profile = apply_proposal_to_profile(profile, proposal, decisions)
    voice_schema.validate("profile", new_profile)


# ---------- update_sources_seen ----------

def test_update_sources_seen_appends_record():
    seen = {"schema_version": 1, "sources": []}
    proposal = make_proposal()
    updated = update_sources_seen(seen, proposal, audience="technical_peer", doc_type="polished",
                                  contributed_exemplar_ids=["ex_aaaaa_001"])
    assert len(updated["sources"]) == 1
    record = updated["sources"][0]
    assert record["hash"] == "a" * 64
    assert record["audience"] == "technical_peer"
    assert record["doc_type"] == "polished"
    assert record["contributed_exemplar_ids"] == ["ex_aaaaa_001"]


def test_update_sources_seen_dedup_by_hash():
    """Re-applying the same proposal doesn't double-record."""
    seen = {"schema_version": 1, "sources": []}
    proposal = make_proposal()
    seen = update_sources_seen(seen, proposal, audience="technical_peer", doc_type="polished",
                               contributed_exemplar_ids=[])
    seen = update_sources_seen(seen, proposal, audience="technical_peer", doc_type="polished",
                               contributed_exemplar_ids=[])
    assert len(seen["sources"]) == 1
