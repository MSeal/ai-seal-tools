"""Tests for proposal data structures, schema validation, and the
run_propose orchestration helper.

The Claude API is mocked throughout — we use the same VoiceLLM injection
pattern as test_voice_llm.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest
import spacy
import yaml

import analyzer
import schema as voice_schema
from analyzer import DescriptorLeak, run_propose
from llm import Classification, VoiceLLM
from proposal import (
    ExemplarWithScrub,
    Proposal,
    list_pending_proposals,
    new_exemplar_id,
    new_proposal_id,
    read_proposal,
    write_proposal,
)
from scrub import ScrubFinding


@pytest.fixture(scope="session")
def nlp():
    try:
        return spacy.load("en_core_web_sm")
    except OSError:
        pytest.skip("en_core_web_sm not installed")


@dataclass
class FakeBlock:
    text: str


@dataclass
class FakeResponse:
    content: list[FakeBlock]


def make_fake_llm(
    classify_payload: dict | None = None,
    descriptors_payload: dict | None = None,
    exemplars_payload: dict | None = None,
) -> VoiceLLM:
    """Build a VoiceLLM with a mock client that returns the given payloads in
    sequence: classify → descriptors → exemplars."""
    classify_payload = classify_payload or {
        "audience": "technical_peer",
        "audience_confidence": 0.9,
        "audience_alternates": [],
        "doc_type": "polished",
        "doc_type_confidence": 0.95,
        "axis_estimates": {
            "formality": 0.6, "technical_density": 0.8, "brevity": 0.4, "warmth": 0.3,
        },
        "reasoning": "test",
    }
    descriptors_payload = descriptors_payload or {
        "voice_summary": "Direct and concise.",
        "rhetorical_moves": ["States the tradeoff before the recommendation"],
        "tics": ["Frequent soft-recommendation markers"],
        "structural_habits": ["Claim, evidence, hedge"],
        "openings_inventory": ["Tradeoff-first opener"],
        "closings_inventory": ["Open-question redirect"],
        "transition_style": "Short pivot sentences.",
        "humor_register": "none observed",
        "self_reference_behavior": "Uses 'we' for team work.",
        "what_to_avoid": ["Avoid bullet lists with parallel verb forms"],
    }
    exemplars_payload = exemplars_payload or {
        "exemplars": [
            {
                "pattern_id": "tradeoff_first",
                "pattern": "Tradeoff-first opener",
                "synthetic": "Choice is X versus Y — X gives speed but cost, Y the reverse.",
                "when_to_use": "When recommending between two alternatives.",
            },
            {
                "pattern_id": "open_question",
                "pattern": "Open-question redirect",
                "synthetic": "Want me to proceed with X, or take a different path?",
                "when_to_use": "When closing a recommendation document.",
            },
        ],
    }
    client = MagicMock()
    responses = [
        FakeResponse(content=[FakeBlock(text=json.dumps(classify_payload))]),
        FakeResponse(content=[FakeBlock(text=json.dumps(descriptors_payload))]),
        FakeResponse(content=[FakeBlock(text=json.dumps(exemplars_payload))]),
    ]
    client.messages = MagicMock()
    client.messages.create = MagicMock(side_effect=responses)
    return VoiceLLM(client=client)


# ---------- IDs --------------------------------------------------------------

def test_new_exemplar_id_format():
    h = "a" * 64
    ex_id = new_exemplar_id(h, 3)
    assert ex_id == "ex_aaaaaa_003"


def test_new_proposal_id_starts_with_prop_and_includes_hash_prefix():
    h = "abcdef0123456789" + "0" * 48
    pid = new_proposal_id(h)
    assert pid.startswith("prop_")
    assert pid.endswith(h[:8])


# ---------- Proposal schema validation ---------------------------------------

def _valid_proposal_dict() -> dict:
    """Minimal valid proposal-v1 dict for schema tests."""
    return {
        "schema_version": 1,
        "proposal_id": "prop_20260524T100000Z_abcdef12",
        "created_at": "2026-05-24T10:00:00Z",
        "source_hash": "a" * 64,
        "source_word_count": 500,
        "classification": {
            "audience": "technical_peer",
            "audience_confidence": 0.9,
            "audience_alternates": [],
            "doc_type": "polished",
            "doc_type_confidence": 0.95,
            "axis_estimates": {
                "formality": 0.6, "technical_density": 0.8, "brevity": 0.4, "warmth": 0.3,
            },
            "reasoning": "test",
        },
        "proposed_stats": {},
        "proposed_descriptors": {},
        "candidate_exemplars": [],
        "scrub_findings": [],
        "review_status": "pending",
    }


def test_proposal_schema_validates_minimal():
    voice_schema.validate("proposal", _valid_proposal_dict())


def test_proposal_schema_rejects_unknown_field():
    bad = _valid_proposal_dict()
    bad["accidental_field"] = True
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("proposal", bad)


def test_proposal_schema_validates_exemplar_with_scrub_findings():
    p = _valid_proposal_dict()
    p["candidate_exemplars"].append({
        "id": "ex_abcdef_001",
        "pattern_id": "x",
        "pattern": "x pattern",
        "synthetic": "Synthetic example",
        "when_to_use": "when relevant",
        "source_hash": "a" * 64,
        "scrub_status": "flagged",
        "scrub_findings": [{
            "rule": "leak:proper_noun",
            "snippet": "Confluent",
            "detail": "Capitalized proper-noun candidate",
            "where": "exemplar:ex_abcdef_001",
        }],
    })
    voice_schema.validate("proposal", p)


def test_proposal_schema_rejects_invalid_doc_type_override():
    p = _valid_proposal_dict()
    p["override_doc_type"] = "manuscript"  # not in enum
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("proposal", p)


def test_proposal_schema_rejects_invalid_review_status():
    p = _valid_proposal_dict()
    p["review_status"] = "maybe_later"
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("proposal", p)


# ---------- write_proposal / read_proposal -----------------------------------

def test_write_and_read_proposal_roundtrip(tmp_path):
    proposal = Proposal(
        proposal_id="prop_test_1",
        created_at="2026-05-24T10:00:00Z",
        source_hash="b" * 64,
        source_word_count=300,
        classification=Classification(
            audience="leadership",
            audience_confidence=0.85,
            audience_alternates=["cross_functional"],
            doc_type="polished",
            doc_type_confidence=0.9,
            axis_estimates={"formality": 0.7, "technical_density": 0.3, "brevity": 0.5, "warmth": 0.4},
            reasoning="status-update register",
        ),
        proposed_stats={"sentence_length": {"mean": 18.4}},
        proposed_descriptors={"voice_summary": "Direct."},
        candidate_exemplars=[],
    )
    path = write_proposal(proposal, dest_dir=tmp_path)
    assert path.is_file()
    loaded = read_proposal(path)
    assert loaded["proposal_id"] == "prop_test_1"
    assert loaded["classification"]["audience"] == "leadership"


def test_list_pending_proposals(tmp_path):
    # Write three proposals — two pending, one accepted
    pending1 = _valid_proposal_dict()
    pending1["proposal_id"] = "prop_a"
    pending1["review_status"] = "pending"

    pending2 = _valid_proposal_dict()
    pending2["proposal_id"] = "prop_b"
    pending2["review_status"] = "pending"

    accepted = _valid_proposal_dict()
    accepted["proposal_id"] = "prop_c"
    accepted["review_status"] = "accepted"

    for p in (pending1, pending2, accepted):
        (tmp_path / f"{p['proposal_id']}.yaml").write_text(yaml.safe_dump(p))

    pendings = list_pending_proposals(tmp_path)
    names = [p.name for p in pendings]
    assert "prop_a.yaml" in names
    assert "prop_b.yaml" in names
    assert "prop_c.yaml" not in names


# ---------- run_propose orchestrator -----------------------------------------

def test_run_propose_happy_path(nlp):
    from lexicons import Lexicons
    text = (
        "We considered several approaches before settling on the simplest. "
        "The team agreed quickly. However, edge cases remain — we should test those next.\n\n"
        "The plan: ship the minimal viable version this week, then iterate."
    )
    llm = make_fake_llm()
    proposal = run_propose(text=text, nlp=nlp, lexicons=Lexicons(), llm=llm)
    assert proposal.classification.audience == "technical_peer"
    assert len(proposal.candidate_exemplars) == 2
    # All exemplars should pass scrub in this clean payload
    assert all(e.scrub_status == "passed" for e in proposal.candidate_exemplars)
    # Exemplar IDs use the hash-prefix format
    for e in proposal.candidate_exemplars:
        assert e.id.startswith("ex_")
    # Stats actually populated
    assert "sentence_length" in proposal.proposed_stats


def test_run_propose_descriptor_leak_raises(nlp):
    """A descriptor that leaks a proper noun from the source must halt."""
    from lexicons import Lexicons
    text = "The team at Confluent shipped the new feature on Thursday."
    leaky_descriptors = {
        "voice_summary": "Crisp and direct.",
        "rhetorical_moves": ["Mentions Confluent prominently"],  # leak!
        "tics": [],
        "structural_habits": [],
        "openings_inventory": [],
        "closings_inventory": [],
        "transition_style": "Short pivots.",
        "humor_register": "none observed",
        "self_reference_behavior": "Uses 'we'.",
        "what_to_avoid": [],
    }
    llm = make_fake_llm(descriptors_payload=leaky_descriptors)
    with pytest.raises(DescriptorLeak) as excinfo:
        run_propose(text=text, nlp=nlp, lexicons=Lexicons(), llm=llm)
    findings = excinfo.value.findings
    assert any("Confluent" in f["snippet"] for f in findings)
    assert any(f["where"].startswith("descriptors:rhetorical_moves") for f in findings)


def test_run_propose_exemplar_leak_flagged_not_raised(nlp):
    """Exemplars that leak get flagged in the proposal; they don't raise."""
    from lexicons import Lexicons
    text = "The team at Acme shipped the new dashboard on Monday."
    leaky_exemplars = {
        "exemplars": [
            {
                "pattern_id": "leak",
                "pattern": "x",
                "synthetic": "The team at Acme made progress.",  # leak: 'Acme' is in source
                "when_to_use": "when relevant",
            },
            {
                "pattern_id": "clean",
                "pattern": "y",
                "synthetic": "Tool A versus Tool B — A is faster, B is simpler.",
                "when_to_use": "when comparing alternatives",
            },
        ],
    }
    llm = make_fake_llm(exemplars_payload=leaky_exemplars)
    proposal = run_propose(text=text, nlp=nlp, lexicons=Lexicons(), llm=llm)
    statuses = {e.pattern_id: e.scrub_status for e in proposal.candidate_exemplars}
    assert statuses["leak"] == "flagged"
    assert statuses["clean"] == "passed"


def test_run_propose_includes_axis_estimates(nlp):
    from lexicons import Lexicons
    llm = make_fake_llm()
    proposal = run_propose(text="A short test sentence.", nlp=nlp, lexicons=Lexicons(), llm=llm)
    axes = proposal.classification.axis_estimates
    assert set(axes.keys()) == {"formality", "technical_density", "brevity", "warmth"}
    for v in axes.values():
        assert 0 <= v <= 1


def test_run_propose_records_overrides(nlp):
    from lexicons import Lexicons
    llm = make_fake_llm()
    proposal = run_propose(
        text="A test document.",
        nlp=nlp,
        lexicons=Lexicons(),
        llm=llm,
        override_audience="leadership",
        override_doc_type="draft",
    )
    assert proposal.override_audience == "leadership"
    assert proposal.override_doc_type == "draft"
    # Classifier still ran and is recorded separately
    assert proposal.classification.audience == "technical_peer"


def test_run_propose_records_source_provenance(nlp):
    """source_type and source_ref pass through run_propose into the
    Proposal dataclass and serialize to the yaml form."""
    from lexicons import Lexicons
    llm = make_fake_llm()
    proposal = run_propose(
        text="A test document.",
        nlp=nlp,
        lexicons=Lexicons(),
        llm=llm,
        source_type="gmail",
        source_ref="https://mail.google.com/mail/u/#inbox/abc123",
    )
    assert proposal.source_type == "gmail"
    assert proposal.source_ref == "https://mail.google.com/mail/u/#inbox/abc123"
    serialized = proposal.as_dict()
    assert serialized["source_type"] == "gmail"
    assert serialized["source_ref"] == "https://mail.google.com/mail/u/#inbox/abc123"


def test_run_propose_omits_provenance_when_not_set(nlp):
    """Provenance is optional — if caller doesn't pass it, those fields
    don't appear in the serialized proposal (so the schema stays clean
    for older Confluence/gdrive paths that haven't been updated yet)."""
    from lexicons import Lexicons
    llm = make_fake_llm()
    proposal = run_propose(
        text="A test document.",
        nlp=nlp,
        lexicons=Lexicons(),
        llm=llm,
    )
    assert proposal.source_type is None
    assert proposal.source_ref is None
    serialized = proposal.as_dict()
    assert "source_type" not in serialized
    assert "source_ref" not in serialized


def test_run_propose_use_when_phrasing_not_flagged(nlp):
    """Regression: each exemplar field gets scrubbed independently so that
    'Use when...' at the start of when_to_use is recognized as sentence-initial
    and not flagged as a 'Use' proper-noun leak.

    First-run on a real document had 8/8 exemplars flagged this way before
    field-by-field scrubbing was introduced.
    """
    from lexicons import Lexicons
    text = "An evaluation document with several sentences. Edge cases noted."
    payload = {
        "exemplars": [{
            "pattern_id": "p",
            "pattern": "States the verdict before the evidence",
            "synthetic": "The choice is between tool A and tool B — tool A is faster.",
            "when_to_use": "Use when readers need the bottom line before context.",
        }],
    }
    llm = make_fake_llm(exemplars_payload=payload)
    proposal = run_propose(text=text, nlp=nlp, lexicons=Lexicons(), llm=llm)
    assert len(proposal.candidate_exemplars) == 1
    assert proposal.candidate_exemplars[0].scrub_status == "passed", \
        f"Use-when phrasing should pass scrub; got findings: {[f.__dict__ for f in proposal.candidate_exemplars[0].scrub_findings]}"


def test_run_propose_written_proposal_validates(tmp_path, nlp):
    """End-to-end: build a proposal and write it; the written file must
    validate against proposal_v1 schema."""
    from lexicons import Lexicons
    text = "A test document with several sentences. It demonstrates structure. Edge cases noted."
    llm = make_fake_llm()
    proposal = run_propose(text=text, nlp=nlp, lexicons=Lexicons(), llm=llm)
    path = write_proposal(proposal, dest_dir=tmp_path)
    # Reading also validates
    loaded = read_proposal(path)
    assert loaded["schema_version"] == 1
    assert len(loaded["candidate_exemplars"]) == 2
