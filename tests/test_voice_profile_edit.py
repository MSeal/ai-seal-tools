"""Tests for skills/voice-review/profile_edit.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "voice-review"
sys.path.insert(0, str(SKILL_DIR))

from profile_edit import (  # noqa: E402
    build_reviewer_notes,
    find_in_archive,
    remove_exemplars,
    restore_exemplar,
    update_reviewer_notes,
)


@pytest.fixture
def sample_profile() -> dict:
    return {
        "last_updated": "2026-06-09T00:00:00Z",
        "audiences": {
            "technical_peer": {
                "sources_count": 2,
                "types": {
                    "polished": {
                        "sources_count": 1,
                        "exemplars": [
                            {"id": "ex_a_001", "pattern": "P1", "synthetic": "S1",
                             "when_to_use": "W1", "reviewer_notes": "n1"},
                            {"id": "ex_a_002", "pattern": "P2", "synthetic": "S2",
                             "when_to_use": "W2", "reviewer_notes": "n2"},
                        ],
                        "descriptors": {"rhetorical_moves": [], "tics": []},
                    },
                    "draft": {
                        "sources_count": 1,
                        "exemplars": [
                            {"id": "ex_b_001", "pattern": "P3", "synthetic": "S3",
                             "when_to_use": "W3", "reviewer_notes": "n3"},
                        ],
                        "descriptors": {"rhetorical_moves": [], "tics": []},
                    },
                },
            },
        },
        "merge_history": [],
    }


# ---------- build_reviewer_notes ----------

def test_build_reviewer_notes_with_notes():
    out = build_reviewer_notes("false_positive:common_word", "common term")
    assert out == "accepted despite scrub flag; reason: false_positive:common_word; notes: common term"


def test_build_reviewer_notes_without_notes():
    out = build_reviewer_notes("false_positive:emphasis", None)
    assert out == "accepted despite scrub flag; reason: false_positive:emphasis"


# ---------- remove_exemplars ----------

def test_remove_single_exemplar(sample_profile: dict):
    removed = remove_exemplars(sample_profile, {"ex_a_001"})
    assert removed == [("ex_a_001", "technical_peer", "polished")]
    remaining_ids = [e["id"] for e in sample_profile["audiences"]["technical_peer"]["types"]["polished"]["exemplars"]]
    assert remaining_ids == ["ex_a_002"]


def test_remove_across_buckets(sample_profile: dict):
    removed = remove_exemplars(sample_profile, {"ex_a_002", "ex_b_001"})
    assert {r[0] for r in removed} == {"ex_a_002", "ex_b_001"}
    polished = sample_profile["audiences"]["technical_peer"]["types"]["polished"]["exemplars"]
    draft = sample_profile["audiences"]["technical_peer"]["types"]["draft"]["exemplars"]
    assert [e["id"] for e in polished] == ["ex_a_001"]
    assert draft == []


def test_remove_no_match(sample_profile: dict):
    removed = remove_exemplars(sample_profile, {"ex_nonexistent"})
    assert removed == []


# ---------- find_in_archive ----------

def _write_archive_proposal(archive_dir: Path, name: str, payload: dict) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(yaml.safe_dump(payload))


def test_find_in_archive_uses_override(tmp_path: Path):
    _write_archive_proposal(tmp_path, "prop_1.yaml", {
        "classification": {"audience": "leadership", "doc_type": "polished"},
        "override_audience": "technical_peer",
        "override_doc_type": "outline",
        "candidate_exemplars": [
            {"id": "ex_x_001", "synthetic": "hello", "pattern": "P", "when_to_use": "W",
             "source_hash": "abc"},
        ],
    })
    result = find_in_archive("ex_x_001", archive_dir=tmp_path)
    assert result is not None
    ex, audience, doc_type = result
    assert audience == "technical_peer"
    assert doc_type == "outline"


def test_find_in_archive_falls_back_to_classification(tmp_path: Path):
    _write_archive_proposal(tmp_path, "prop_2.yaml", {
        "classification": {"audience": "self_notes", "doc_type": "draft"},
        "candidate_exemplars": [
            {"id": "ex_y_001", "synthetic": "hi", "pattern": "P", "when_to_use": "W",
             "source_hash": "def"},
        ],
    })
    result = find_in_archive("ex_y_001", archive_dir=tmp_path)
    assert result is not None
    _, audience, doc_type = result
    assert audience == "self_notes"
    assert doc_type == "draft"


def test_find_in_archive_returns_none_for_missing(tmp_path: Path):
    assert find_in_archive("ex_nope", archive_dir=tmp_path) is None


# ---------- restore_exemplar ----------

def test_restore_exemplar_adds_to_profile(sample_profile: dict, tmp_path: Path):
    _write_archive_proposal(tmp_path, "prop_r.yaml", {
        "classification": {"audience": "technical_peer", "doc_type": "polished"},
        "candidate_exemplars": [
            {"id": "ex_restore_001", "synthetic": "Restored content",
             "pattern": "P", "when_to_use": "W", "source_hash": "hash1"},
        ],
    })
    result = restore_exemplar(sample_profile, "ex_restore_001",
                              reason="true_positive:override",
                              notes="user override",
                              archive_dir=tmp_path,
                              now="2026-06-09T12:00:00Z")
    assert result == ("technical_peer", "polished")
    exs = sample_profile["audiences"]["technical_peer"]["types"]["polished"]["exemplars"]
    restored = next(e for e in exs if e["id"] == "ex_restore_001")
    assert restored["synthetic"] == "Restored content"
    assert restored["reviewer_notes"].startswith("accepted despite scrub flag; reason: true_positive:override")
    assert restored["reviewed_at"] == "2026-06-09T12:00:00Z"


def test_restore_exemplar_skips_if_already_present(sample_profile: dict, tmp_path: Path):
    _write_archive_proposal(tmp_path, "prop_dup.yaml", {
        "classification": {"audience": "technical_peer", "doc_type": "polished"},
        "candidate_exemplars": [
            {"id": "ex_a_001", "synthetic": "duplicate", "pattern": "P",
             "when_to_use": "W", "source_hash": "hash"},
        ],
    })
    result = restore_exemplar(sample_profile, "ex_a_001", reason="false_positive:other",
                              archive_dir=tmp_path)
    assert result is None
    # Original ex_a_001 unchanged
    exs = sample_profile["audiences"]["technical_peer"]["types"]["polished"]["exemplars"]
    ex_a = next(e for e in exs if e["id"] == "ex_a_001")
    assert ex_a["synthetic"] == "S1"


def test_restore_exemplar_with_substitute(sample_profile: dict, tmp_path: Path):
    """With substitute=True, numbers get replaced before the exemplar is added."""
    _write_archive_proposal(tmp_path, "prop_sub.yaml", {
        "classification": {"audience": "technical_peer", "doc_type": "draft"},
        "candidate_exemplars": [
            {"id": "ex_sub_001",
             "synthetic": "34 Teams Onboarded in 2025",
             "pattern": "P",
             "when_to_use": "W",
             "source_hash": "hash",
             "scrub_findings": [],
            },
        ],
    })
    result = restore_exemplar(sample_profile, "ex_sub_001",
                              reason="false_positive:other",
                              substitute=True,
                              archive_dir=tmp_path)
    assert result == ("technical_peer", "draft")
    restored = next(e for e in sample_profile["audiences"]["technical_peer"]["types"]["draft"]["exemplars"]
                    if e["id"] == "ex_sub_001")
    assert "34" not in restored["synthetic"]
    assert "<N>" in restored["synthetic"]
    assert "<YYYY>" in restored["synthetic"]


def test_restore_exemplar_returns_none_when_not_in_archive(sample_profile: dict, tmp_path: Path):
    assert restore_exemplar(sample_profile, "ex_missing", reason="x", archive_dir=tmp_path) is None


# ---------- update_reviewer_notes ----------

def test_update_reviewer_notes_updates_in_place(sample_profile: dict):
    result = update_reviewer_notes(sample_profile, "ex_a_002",
                                   reason="false_positive:emphasis",
                                   notes="bold styling",
                                   now="2026-06-09T13:00:00Z")
    assert result == ("technical_peer", "polished")
    ex = next(e for e in sample_profile["audiences"]["technical_peer"]["types"]["polished"]["exemplars"]
              if e["id"] == "ex_a_002")
    assert ex["reviewer_notes"] == "accepted despite scrub flag; reason: false_positive:emphasis; notes: bold styling"
    assert ex["reviewed_at"] == "2026-06-09T13:00:00Z"


def test_update_reviewer_notes_returns_none_when_missing(sample_profile: dict):
    assert update_reviewer_notes(sample_profile, "ex_nope", reason="x") is None
