"""Tests for the voice-analyze schema/migration framework.

Cover:
- Templates validate against the latest schema.
- Unknown fields fail validation (the proactive-drift guard).
- migrate_forward is a no-op at the latest version.
- migrate_forward rejects data at a future version.
- migrate_forward fails loudly when a needed migration file is missing.
- Required cross-cutting constraints: audience_registry contains the
  expected initial tags; doc_type enum accepts {polished, draft, outline}.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

import schema as voice_schema

SKILL_DIR = Path(voice_schema.SKILL_DIR)
TEMPLATES = SKILL_DIR / "templates"
SCHEMAS = SKILL_DIR / "schemas"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_profile_template_validates_against_v1():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    voice_schema.validate("profile", data)


def test_sources_seen_template_validates_against_v1():
    data = _load_yaml(TEMPLATES / "sources_seen.yaml")
    voice_schema.validate("sources_seen", data)


def test_latest_schema_files_exist():
    assert (SCHEMAS / f"profile_v{voice_schema.LATEST_PROFILE_SCHEMA}.json").is_file()
    assert (SCHEMAS / f"sources_seen_v{voice_schema.LATEST_SOURCES_SEEN_SCHEMA}.json").is_file()
    assert (SCHEMAS / f"proposal_v{voice_schema.LATEST_PROPOSAL_SCHEMA}.json").is_file()


def test_valid_audiences_matches_template_registry():
    """lexicons.VALID_AUDIENCES must equal the template's audience_registry keys."""
    from lexicons import VALID_AUDIENCES
    data = _load_yaml(TEMPLATES / "profile.yaml")
    assert set(data["audience_registry"]) == set(VALID_AUDIENCES)


def test_proposal_kind_in_valid_kinds():
    assert "proposal" in voice_schema.VALID_KINDS
    assert voice_schema.latest("proposal") == voice_schema.LATEST_PROPOSAL_SCHEMA


def test_unknown_top_level_field_fails():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["unexpected_field"] = "boom"
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("profile", data)


def test_unknown_stats_field_fails():
    """The proactive-drift guard: adding a stat without a schema bump must fail."""
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["audiences"]["technical_peer"] = {
        "description": "test",
        "sources_count": 1,
        "axis_baseline": {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5},
        "types": {
            "polished": {
                "sources_count": 1,
                "stats": {"new_unauthorized_marker": 0.42},
                "descriptors": {},
                "exemplars": [],
            }
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("profile", data)


def test_invalid_doc_type_fails():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["audiences"]["technical_peer"] = {
        "description": "test",
        "sources_count": 1,
        "axis_baseline": {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5},
        "types": {
            "wishlist": {  # not in {polished, draft, outline}
                "sources_count": 1,
                "stats": {},
                "descriptors": {},
                "exemplars": [],
            }
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("profile", data)


@pytest.mark.parametrize("doc_type", ["polished", "draft", "outline", "chat"])
def test_all_doc_types_accepted(doc_type):
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["audiences"]["technical_peer"] = {
        "description": "test",
        "sources_count": 1,
        "axis_baseline": {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5},
        "types": {
            doc_type: {
                "sources_count": 1,
                "stats": {},
                "descriptors": {},
                "exemplars": [],
            }
        },
    }
    voice_schema.validate("profile", data)


def test_audience_registry_has_expected_initial_tags():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    expected = {
        "technical_peer",
        "leadership",
        "direct_report",
        "cross_functional",
        "external_public",
        "casual",
        "self_notes",
    }
    assert set(data["audience_registry"]) == expected, (
        "Initial audience tag set drifted from the agreed v1 design."
    )


def test_sources_seen_doc_type_enum():
    data = _load_yaml(TEMPLATES / "sources_seen.yaml")
    data["sources"].append({
        "hash": "a" * 64,
        "analyzed_at": "2026-05-24T10:00:00Z",
        "schema_version_at_time": 1,
        "audience": "technical_peer",
        "doc_type": "outline",  # the new doc_type field added for outline-vs-polished distinction
        "word_count": 800,
    })
    voice_schema.validate("sources_seen", data)


def test_sources_seen_hash_pattern_enforced():
    data = _load_yaml(TEMPLATES / "sources_seen.yaml")
    data["sources"].append({
        "hash": "not-a-hash",
        "analyzed_at": "2026-05-24T10:00:00Z",
        "schema_version_at_time": 1,
        "audience": "technical_peer",
        "doc_type": "polished",
        "word_count": 800,
    })
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("sources_seen", data)


def test_exemplar_requires_source_hash():
    """Every exemplar must be linked back to its originating source via hash."""
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["audiences"]["technical_peer"] = {
        "description": "test",
        "sources_count": 1,
        "axis_baseline": {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5, "warmth": 0.5},
        "types": {
            "polished": {
                "sources_count": 1,
                "stats": {},
                "descriptors": {},
                "exemplars": [
                    {
                        "id": "ex_001",
                        # missing source_hash
                        "pattern": "Tradeoff-first opener",
                        "synthetic": "The fork is X vs Y — X gives A but B, Y the reverse.",
                        "when_to_use": "Recommendation prompts",
                        "reviewed_at": "2026-05-24T10:00:00Z",
                    },
                ],
            }
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("profile", data)


def test_migrate_forward_noop_at_latest():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    migrated = voice_schema.migrate_forward("profile", data)
    assert migrated["schema_version"] == voice_schema.LATEST_PROFILE_SCHEMA
    # Same data dict (no-op)
    voice_schema.validate("profile", migrated)


def test_migrate_forward_rejects_future_version():
    data = _load_yaml(TEMPLATES / "profile.yaml")
    data["schema_version"] = voice_schema.LATEST_PROFILE_SCHEMA + 1
    with pytest.raises(ValueError, match="newer than this code supports"):
        voice_schema.migrate_forward("profile", data)


def test_migrate_forward_missing_migration_fails(tmp_path, monkeypatch):
    """If LATEST jumps but the migration file isn't there, fail loudly."""
    data = _load_yaml(TEMPLATES / "profile.yaml")
    # Pretend the latest version is 2, but no v1->v2 migration exists.
    monkeypatch.setattr(voice_schema, "LATEST_PROFILE_SCHEMA", 2)
    with pytest.raises(RuntimeError, match="Missing migration"):
        voice_schema.migrate_forward("profile", data)


def test_anti_pattern_derived_requires_metric_field_for_lint(tmp_path):
    """If derived=true, derived_from_metric should be present in practice.

    The schema doesn't enforce this conditional (would need 'if/then' which
    works but adds complexity); this test documents the invariant so a
    future change to require it is caught.
    """
    data = _load_yaml(TEMPLATES / "profile.yaml")
    for ap in data["shared_anti_patterns"]:
        if ap.get("derived"):
            assert "derived_from_metric" in ap, (
                f"derived anti-pattern {ap['id']} should declare derived_from_metric"
            )
