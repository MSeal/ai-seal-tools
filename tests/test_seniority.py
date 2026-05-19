"""Tests for seniority.py — title-to-tier inference, YAML loader, lookup."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

import seniority as sn


def _f(title=None, **kw) -> sn.SeniorityFields:
    """Build a SeniorityFields with email=test@x and the given title."""
    return sn.SeniorityFields(email="test@x", title=title, **kw)


# ---------------------------------------------------------------------------
# infer_tier — title-prefix corpus
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, expected_tier", [
    # Tier 5 — C-level / SVP/EVP
    ("Chief Technology Officer", 5),
    ("CTO", 5),
    ("CEO", 5),
    ("Chief Information Security Officer", 5),
    ("SVP, Engineering", 5),
    ("Senior Vice President, Product", 5),
    ("Executive Vice President", 5),
    # Tier 4 — VP
    ("VP, Engineering", 4),
    ("Vice President of Sales", 4),
    # Tier 3 — Director
    ("Director, Platform", 3),
    ("Director II, Engineering", 3),
    ("Senior Director, AI", 3),
    # Tier 2 — Principal IC / Senior Manager
    ("Principal Engineer II", 2),
    ("Distinguished Engineer", 2),
    ("Senior Manager, Backend", 2),
    ("Sr Manager", 2),
    ("Software Architect", 2),
    # Tier 1 — Senior IC / first-line manager
    ("Staff Engineer", 1),
    ("Senior Software Engineer", 1),
    ("Lead Engineer", 1),
    ("Engineering Manager", 1),
    ("Manager, Backend", 1),
    ("Head of Recruiting", 1),
    # Tier 0 — base IC / unmatched
    ("Software Engineer", 0),
    ("Data Scientist", 0),
    ("Analyst", 0),
    ("Designer", 0),
])
def test_infer_tier_title_corpus(title, expected_tier):
    assert sn.infer_tier(_f(title=title)) == expected_tier


def test_infer_tier_empty_or_missing_title():
    assert sn.infer_tier(_f(title=None)) == 0
    assert sn.infer_tier(_f(title="")) == 0
    assert sn.infer_tier(_f(title="   ")) == 0


def test_infer_tier_first_match_wins():
    """Title containing both 'VP' and 'Director' classifies as the more
    senior (VP, tier 4) because VP rule fires before Director."""
    assert sn.infer_tier(_f(title="VP & Acting Director, Cloud")) == 4


# ---------------------------------------------------------------------------
# load_seniority — YAML parsing
# ---------------------------------------------------------------------------

def test_load_seniority_missing_file_empty(tmp_path):
    assert sn.load_seniority(tmp_path / "no.yaml") == {}


def test_load_seniority_accepts_bare_integer_entries(tmp_path):
    """Hand-curated minimal entries: `email: <tier>` is valid YAML and
    normalizes to {'tier': N}."""
    path = tmp_path / "s.yaml"
    path.write_text("seniority:\n  alice@x: 4\n  bob@x: 2\n")
    out = sn.load_seniority(path)
    assert out == {"alice@x": {"tier": 4}, "bob@x": {"tier": 2}}


def test_load_seniority_accepts_rich_records(tmp_path):
    """Records can also carry title/source/etc.; load preserves them."""
    path = tmp_path / "s.yaml"
    path.write_text(
        "seniority:\n"
        "  alice@x:\n"
        "    tier: 4\n"
        "    title: VP, Engineering\n"
        "    department: Engineering\n"
        "    source: glean\n"
    )
    out = sn.load_seniority(path)
    assert out["alice@x"]["tier"] == 4
    assert out["alice@x"]["title"] == "VP, Engineering"
    assert out["alice@x"]["source"] == "glean"


def test_load_seniority_lowercases_emails(tmp_path):
    """Email keys are lowercased for case-insensitive lookups."""
    path = tmp_path / "s.yaml"
    path.write_text("seniority:\n  Alice@Example.com: 4\n")
    out = sn.load_seniority(path)
    assert "alice@example.com" in out
    assert "Alice@Example.com" not in out


def test_load_seniority_skips_malformed(tmp_path, capsys):
    """Malformed entries (no tier) → skipped with stderr note; rest still loads."""
    path = tmp_path / "s.yaml"
    path.write_text(
        "seniority:\n"
        "  alice@x: 4\n"
        "  bob@x:\n"
        "    title: VP\n"  # missing 'tier'
        "  carol@x: 'not a number'\n"
    )
    out = sn.load_seniority(path)
    assert "alice@x" in out
    assert "bob@x" not in out
    err = capsys.readouterr().err
    assert "malformed" in err


def test_tier_for_lookup():
    seniority = {"alice@x": {"tier": 4}, "bob@x": {"tier": 0}}
    assert sn.tier_for("alice@x", seniority) == 4
    assert sn.tier_for("Alice@X", seniority) == 4  # case-insensitive
    assert sn.tier_for("bob@x", seniority) == 0
    assert sn.tier_for("nobody@x", seniority) == 0


# ---------------------------------------------------------------------------
# write_record — idempotent YAML write
# ---------------------------------------------------------------------------

def test_write_record_creates_file_with_inferred_tier(tmp_path):
    path = tmp_path / "s.yaml"
    fields = sn.SeniorityFields(email="alice@x", title="VP, Engineering", source="manual")
    sn.write_record(path, fields)
    out = sn.load_seniority(path)
    assert out["alice@x"]["tier"] == 4   # inferred from "VP"
    assert out["alice@x"]["title"] == "VP, Engineering"
    assert out["alice@x"]["source"] == "manual"
    assert "fetched_at" in out["alice@x"]


def test_write_record_respects_explicit_tier(tmp_path):
    """Caller can pass tier explicitly to override inference."""
    path = tmp_path / "s.yaml"
    fields = sn.SeniorityFields(email="alice@x", title="Engineer", source="manual")
    sn.write_record(path, fields, tier=5)   # claim C-level despite IC title
    assert sn.load_seniority(path)["alice@x"]["tier"] == 5


def test_write_record_replaces_existing_entry(tmp_path):
    """Re-writing the same email overwrites the prior record (latest wins)."""
    path = tmp_path / "s.yaml"
    sn.write_record(path, sn.SeniorityFields(email="alice@x", title="Engineer"), tier=1)
    sn.write_record(path, sn.SeniorityFields(email="alice@x", title="VP", source="glean"))
    out = sn.load_seniority(path)
    assert out["alice@x"]["tier"] == 4
    assert out["alice@x"]["source"] == "glean"


def test_write_record_preserves_other_entries(tmp_path):
    """Writing one email shouldn't drop existing entries for other emails."""
    path = tmp_path / "s.yaml"
    sn.write_record(path, sn.SeniorityFields(email="alice@x", title="VP"))
    sn.write_record(path, sn.SeniorityFields(email="bob@x", title="Director"))
    out = sn.load_seniority(path)
    assert out["alice@x"]["tier"] == 4
    assert out["bob@x"]["tier"] == 3
