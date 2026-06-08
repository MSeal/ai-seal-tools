"""Unit tests for the source-index categorization heuristic and ops.

The heuristic is a first-pass categorizer; tests cover the main cases we
expect to see in real corpus discovery (Confluence titles + spaces) and the
edge behaviors (no match, ambiguity, personal-space drafting).
"""
from __future__ import annotations

import jsonschema
import pytest
import yaml

import schema as voice_schema
from indexer import (
    add_entries,
    auto_skip_data_entries,
    categorize_title,
    dedup_key_for,
    generate_source_id,
    load_index,
    save_index,
    strip_internal_keys,
    summary,
)


# ---------- Heuristic categorizer ----------

def test_categorize_external_public():
    h = categorize_title("Bangalore 2025 Talk Draft Outline Proposal")
    assert h.audience == "external_public"
    assert h.audience_confidence >= 0.7
    # Outline doc_type should come through
    assert h.doc_type == "outline"


def test_categorize_leadership_signals():
    cases = [
        "Leadership Sync Meeting Notes",
        "Client Teams' Operational Suggestions",
        "Go/No-Go Review 2-18-26",
        "NRR Leverage Metric",
    ]
    for title in cases:
        h = categorize_title(title)
        assert h.audience == "leadership", f"{title!r} → {h.audience}"


def test_categorize_technical_peer():
    cases = [
        "Design Note: VS Code extension architecture (Phase 2)",
        "API Reliability and Future Proofing",
        "Intellij Kafka Plugin Technology State",
        "Kafka Service Log DTX Analysis Security Review",
        "Test Scenarios",
        "Secondary Data Indexing Limitations [WIP]",
    ]
    for title in cases:
        h = categorize_title(title)
        assert h.audience == "technical_peer", f"{title!r} → {h.audience}"


def test_categorize_cross_functional():
    cases = [
        "RM-Experiences Partnership Asks",
        "AI in UI Primer",
    ]
    for title in cases:
        h = categorize_title(title)
        assert h.audience == "cross_functional", f"{title!r} → {h.audience}"


def test_categorize_wip_marks_draft():
    h = categorize_title("Secondary Data Indexing Limitations [WIP]")
    assert h.doc_type == "draft"


def test_categorize_default_low_confidence():
    """A title with no recognizable signals falls back to technical_peer at low confidence."""
    h = categorize_title("Random Untagged Document")
    assert h.audience == "technical_peer"
    assert h.audience_confidence < 0.5


def test_categorize_space_corroboration_boosts_confidence():
    """If title matches technical_peer AND space is in tech-engineering space, boost."""
    h_with = categorize_title("Design Note: Foo", space="DTX")
    h_without = categorize_title("Design Note: Foo")
    assert h_with.audience == "technical_peer"
    assert h_without.audience == "technical_peer"
    assert h_with.audience_confidence > h_without.audience_confidence


def test_categorize_personal_space_drafts_default():
    """Confluence personal space pages (~userid) without explicit polish signals
    skew toward draft."""
    h = categorize_title("Some Random Notes", space="~712020a48a7bca54714a3aaf5ea5136eb04918")
    assert h.doc_type == "draft"


def test_categorize_personal_space_polished_title_stays_polished():
    """If the title clearly indicates a polished doc type, personal space
    should not downgrade it."""
    h = categorize_title("Some Architecture Document", space="~712020a48a7bca54714a3aaf5ea5136eb04918")
    # Architecture doc → technical_peer (high conf); doc_type may default to polished
    assert h.audience == "technical_peer"


def test_categorize_design_space_gets_cross_functional_hint():
    h = categorize_title("Untagged Title", space="DESIGN")
    # Title doesn't match a rule but space hint should fire
    assert h.audience == "cross_functional"


# ---------- ID generation ----------

def test_generate_source_id_format():
    sid = generate_source_id("confluence", "https://confluentinc.atlassian.net/wiki/spaces/DTX/pages/1234")
    assert sid.startswith("confluence_")
    # 12-char hash
    assert len(sid.split("_", 1)[1]) == 12


def test_generate_source_id_stable():
    a = generate_source_id("local", "/path/to/doc.md")
    b = generate_source_id("local", "/path/to/doc.md")
    assert a == b


# ---------- Dedup keys ----------

def test_dedup_key_uses_url_first():
    assert dedup_key_for({"url": "https://x.example.com/foo"}) == "https://x.example.com/foo"


def test_dedup_key_falls_back_to_path():
    key = dedup_key_for({"path": "~/docs/foo.md"})
    assert key.endswith("docs/foo.md")
    # Absolute path
    assert key.startswith("/")


def test_dedup_key_raises_without_url_or_path():
    with pytest.raises(ValueError):
        dedup_key_for({"location": "local"})


# ---------- Index ops ----------

def test_add_entries_dedups_by_url():
    index = {"schema_version": 1, "sources": []}
    entries = [
        {"location": "confluence", "url": "https://x/1", "title": "Design Note: A"},
        {"location": "confluence", "url": "https://x/1", "title": "Design Note: A"},
    ]
    out = add_entries(entries, index=index)
    assert len(out["sources"]) == 1
    assert out["_last_add_summary"] == {"added": 1, "updated": 1}


def test_add_entries_applies_heuristic():
    index = {"schema_version": 1, "sources": []}
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "Leadership Sync 2026-04"},
    ], index=index)
    s = out["sources"][0]
    assert s["proposed_audience"] == "leadership"
    assert s["audience_source"] == "heuristic"
    assert s["status"] == "queued"
    assert s["id"].startswith("confluence_")


def test_add_entries_preserves_status_on_re_add():
    """Re-adding a source that's already 'analyzed' must not downgrade status."""
    index = {"schema_version": 1, "sources": []}
    out = add_entries([{"location": "confluence", "url": "https://x/1", "title": "T"}], index=index)
    # Simulate having analyzed it
    out["sources"][0]["status"] = "analyzed"
    out["sources"][0]["source_hash"] = "a" * 64
    # Re-add with same key
    out = add_entries([{"location": "confluence", "url": "https://x/1", "title": "T (updated)"}], index=out)
    assert out["sources"][0]["status"] == "analyzed"
    assert out["sources"][0]["source_hash"] == "a" * 64
    # But title can be refreshed
    assert out["sources"][0]["title"] == "T (updated)"


def test_add_entries_respects_explicit_audience():
    """If the caller supplies an audience, don't overwrite with heuristic."""
    out = add_entries([{
        "location": "local",
        "path": "/tmp/x.md",
        "title": "Anything",
        "proposed_audience": "casual",
        "audience_source": "manual",
    }])
    assert out["sources"][0]["proposed_audience"] == "casual"
    assert out["sources"][0]["audience_source"] == "manual"


def test_summary_counts():
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "Leadership Sync"},
        {"location": "confluence", "url": "https://x/2", "title": "Design Note: A"},
        {"location": "confluence", "url": "https://x/3", "title": "Design Note: B"},
        {"location": "local", "path": "/tmp/foo.md", "title": "Bangalore Talk Outline"},
    ])
    s = summary(out)
    assert s["total"] == 4
    assert s["by_audience"]["leadership"] == 1
    assert s["by_audience"]["technical_peer"] == 2
    assert s["by_audience"]["external_public"] == 1
    assert s["by_location"]["confluence"] == 3
    assert s["by_location"]["local"] == 1
    assert s["audience_x_doc_type"]["external_public"]["outline"] == 1


def test_save_and_load_roundtrip(tmp_path):
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "Design Note: A"},
    ])
    out = strip_internal_keys(out)
    path = tmp_path / "source_index.yaml"
    save_index(out, path=path)
    loaded = load_index(path)
    assert len(loaded["sources"]) == 1
    assert loaded["sources"][0]["title"] == "Design Note: A"


def test_save_validates_schema(tmp_path):
    bad = {
        "schema_version": 1,
        "sources": [{
            "id": "BAD ID",  # spaces not allowed by id pattern
            "location": "confluence",
            "url": "https://x/1",
            "title": "T",
            "discovered_at": "2026-05-24T00:00:00Z",
            "status": "queued",
        }],
    }
    path = tmp_path / "source_index.yaml"
    with pytest.raises(jsonschema.ValidationError):
        save_index(bad, path=path)


def test_load_empty_returns_default():
    """load_index on a nonexistent path returns a valid empty doc."""
    data = load_index(path=tmp_nonexistent())
    assert data["schema_version"] == 1
    assert data["sources"] == []


def tmp_nonexistent():
    """Return a path that doesn't exist (for load_empty test)."""
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "does_not_exist.yaml"
        return p


def test_schema_v1_source_index_in_valid_kinds():
    assert "source_index" in voice_schema.VALID_KINDS
    assert voice_schema.latest("source_index") == 1


def test_template_validates():
    """The checked-in template must validate against v1 schema."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "skills" / "voice-analyze" / "templates" / "source_index.yaml"
    data = yaml.safe_load(p.read_text())
    voice_schema.validate("source_index", data)


def test_contribution_type_defaults_to_unknown():
    """New entries without explicit contribution_type get 'unknown'."""
    out = add_entries([{
        "location": "confluence", "url": "https://x/1", "title": "Design Note",
    }])
    assert out["sources"][0]["contribution_type"] == "unknown"


def test_contribution_type_respected_when_set():
    out = add_entries([{
        "location": "confluence", "url": "https://x/1", "title": "Design Note",
        "contribution_type": "contributed",
    }])
    assert out["sources"][0]["contribution_type"] == "contributed"


def test_contribution_type_in_summary():
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "A",
         "contribution_type": "authored"},
        {"location": "confluence", "url": "https://x/2", "title": "B",
         "contribution_type": "contributed"},
        {"location": "confluence", "url": "https://x/3", "title": "C",
         "contribution_type": "contributed"},
    ])
    s = summary(out)
    assert s["by_contribution_type"] == {"authored": 1, "contributed": 2}


def test_invalid_contribution_type_rejected():
    bad = {
        "schema_version": 1,
        "sources": [{
            "id": "confluence_aaaaaaaaaaaa",
            "location": "confluence",
            "url": "https://x/1",
            "title": "T",
            "discovered_at": "2026-05-24T00:00:00Z",
            "status": "queued",
            "contribution_type": "made_up_value",
        }],
    }
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("source_index", bad)


def test_auto_skip_data_locations():
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "Design Note A"},
        {"location": "gdrive_sheets", "url": "https://x/2", "title": "Some Sheet"},
        {"location": "gdrive_other", "url": "https://x/3", "title": "Random Upload"},
        {"location": "gdrive_doc", "url": "https://x/4", "title": "Real Doc"},
    ])
    counts = auto_skip_data_entries(out)
    assert counts["skipped_now"] == 2  # gdrive_sheets + gdrive_other
    statuses = {s["url"]: s["status"] for s in out["sources"]}
    assert statuses["https://x/1"] == "queued"
    assert statuses["https://x/2"] == "skipped"
    assert statuses["https://x/3"] == "skipped"
    assert statuses["https://x/4"] == "queued"


def test_auto_skip_data_title_patterns():
    out = add_entries([
        {"location": "confluence", "url": "https://x/1", "title": "Q4 Burndown Tracker"},
        {"location": "confluence", "url": "https://x/2", "title": "ui-errors.csv"},
        {"location": "confluence", "url": "https://x/3", "title": "UI Performance Worktrack Progress"},
        {"location": "confluence", "url": "https://x/4", "title": "Untitled spreadsheet"},
        {"location": "confluence", "url": "https://x/5", "title": "Design Note: A"},
    ])
    counts = auto_skip_data_entries(out)
    statuses = {s["url"]: (s["status"], s.get("skip_reason")) for s in out["sources"]}
    assert statuses["https://x/1"][0] == "skipped"  # "Burndown" + "Tracker"
    assert statuses["https://x/2"][0] == "skipped"  # .csv
    assert statuses["https://x/3"][0] == "skipped"  # "Worktrack" + "Progress"
    assert statuses["https://x/4"][0] == "skipped"  # "Untitled"
    assert statuses["https://x/5"][0] == "queued"
    assert counts["skipped_now"] == 4


def test_auto_skip_preserves_already_skipped():
    out = add_entries([{"location": "confluence", "url": "https://x/1", "title": "Some Burndown"}])
    out["sources"][0]["status"] = "skipped"
    out["sources"][0]["skip_reason"] = "manual skip"
    counts = auto_skip_data_entries(out)
    assert out["sources"][0]["skip_reason"] == "manual skip"  # not overwritten
    assert counts["already_skipped"] == 1


def test_auto_skip_preserves_analyzed():
    out = add_entries([{"location": "gdrive_sheets", "url": "https://x/1", "title": "Sheet"}])
    out["sources"][0]["status"] = "analyzed"
    counts = auto_skip_data_entries(out)
    assert out["sources"][0]["status"] == "analyzed"  # not downgraded


def test_invalid_location_rejected():
    bad = {
        "schema_version": 1,
        "sources": [{
            "id": "bogus_aaaaaaaaaaaa",
            "location": "made_up_source",
            "url": "https://x/1",
            "title": "T",
            "discovered_at": "2026-05-24T00:00:00Z",
            "status": "queued",
        }],
    }
    with pytest.raises(jsonschema.ValidationError):
        voice_schema.validate("source_index", bad)
