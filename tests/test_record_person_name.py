"""Tests for record_person_name — writes email→name pairs into
people.yaml after Glean lookups. Pure-function coverage of merge math
(idempotency, name-not-clobbered-by-null, source list dedup) plus
CLI smoke tests for single + bulk modes via subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

import record_person_name as rpn


SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "record_person_name.py"


# ---------------------------------------------------------------------------
# load — defensive on disk shapes
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_skeleton(tmp_path):
    assert rpn.load(tmp_path / "people.yaml") == {"people": {}}


def test_load_empty_file_returns_skeleton(tmp_path):
    p = tmp_path / "people.yaml"
    p.write_text("")
    assert rpn.load(p) == {"people": {}}


def test_load_template_only_returns_skeleton(tmp_path):
    """Matches the committed template's `people: {}` payload."""
    p = tmp_path / "people.yaml"
    p.write_text("people: {}\n")
    assert rpn.load(p) == {"people": {}}


def test_load_existing_entries_preserved(tmp_path):
    p = tmp_path / "people.yaml"
    p.write_text("people:\n  alice@example.com:\n    name: Alice Example\n")
    data = rpn.load(p)
    assert data["people"]["alice@example.com"]["name"] == "Alice Example"


# ---------------------------------------------------------------------------
# merge_name — core logic
# ---------------------------------------------------------------------------


def test_merge_adds_new_entry():
    data: dict = {"people": {}}
    changed = rpn.merge_name(data, "alice@example.com", "Alice Example",
                             source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert changed is True
    assert data["people"]["alice@example.com"]["name"] == "Alice Example"
    assert data["people"]["alice@example.com"]["sources"] == ["glean"]


def test_merge_lowercases_email_key():
    """Email casing varies across sources; the cache normalizes to
    lowercase so subsequent lookups are case-insensitive."""
    data: dict = {"people": {}}
    rpn.merge_name(data, "Alice@Example.com", "Alice",
                   source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert "alice@example.com" in data["people"]
    assert "Alice@Example.com" not in data["people"]


def test_merge_no_clobber_when_name_is_null():
    """Glean returned null (couldn't find a name) — don't wipe an
    existing cached name."""
    data: dict = {"people": {"alice@example.com": {"name": "Alice Example", "sources": ["glean"]}}}
    changed = rpn.merge_name(data, "alice@example.com", None,
                             source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert changed is False
    assert data["people"]["alice@example.com"]["name"] == "Alice Example"


def test_merge_updates_existing_name():
    """If Glean returns a different (presumably-newer) name for an
    existing entry, update it and mark name_fetched_at."""
    data: dict = {
        "people": {
            "alice@example.com": {
                "name": "Old Name",
                "sources": ["calendar-scan"],
                "name_fetched_at": "2026-04-01T00:00:00+00:00",
            }
        }
    }
    changed = rpn.merge_name(data, "alice@example.com", "Alice Example",
                             source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert changed is True
    entry = data["people"]["alice@example.com"]
    assert entry["name"] == "Alice Example"
    assert entry["name_fetched_at"] == "2026-05-18T00:00:00+00:00"


def test_merge_dedupes_sources_list():
    data: dict = {"people": {"alice@example.com": {"name": "Alice", "sources": ["glean"]}}}
    rpn.merge_name(data, "alice@example.com", "Alice",
                   source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert data["people"]["alice@example.com"]["sources"] == ["glean"]


def test_merge_appends_new_source():
    """A second source (e.g., calendar-scan after glean) is appended."""
    data: dict = {"people": {"alice@example.com": {"name": "Alice", "sources": ["calendar-scan"]}}}
    rpn.merge_name(data, "alice@example.com", "Alice",
                   source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert data["people"]["alice@example.com"]["sources"] == ["calendar-scan", "glean"]


def test_merge_skips_blank_email():
    """A stray '' key from a malformed JSON payload doesn't end up in
    the cache."""
    data: dict = {"people": {}}
    changed = rpn.merge_name(data, "   ", "Nameless",
                             source="glean", fetched_at="2026-05-18T00:00:00+00:00")
    assert changed is False
    assert data["people"] == {}


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_cli_single_mode_writes_entry(tmp_path):
    p = tmp_path / "people.yaml"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p),
         "single", "--email", "alice@example.com", "--name", "Alice Example"],
        capture_output=True, text=True, env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["names_updated"] == 1
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["people"]["alice@example.com"]["name"] == "Alice Example"


def test_cli_bulk_mode_via_stdin(tmp_path):
    p = tmp_path / "people.yaml"
    payload = {"alice@example.com": "Alice Example", "bob@example.com": "Bob Example"}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p), "bulk"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["names_updated"] == 2
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["people"]["alice@example.com"]["name"] == "Alice Example"
    assert on_disk["people"]["bob@example.com"]["name"] == "Bob Example"


def test_cli_bulk_mode_tolerates_null_names(tmp_path):
    """Glean's null for unknown names → cache entry still recorded
    (without a name field) so a later run can fill it in."""
    p = tmp_path / "people.yaml"
    payload = {"alice@example.com": "Alice", "unknown@example.com": None}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p), "bulk"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["names_updated"] == 1  # only alice's name landed
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["people"]["alice@example.com"]["name"] == "Alice"


def test_cli_bulk_mode_rejects_non_object_stdin(tmp_path):
    p = tmp_path / "people.yaml"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p), "bulk"],
        input="[]",  # JSON array, not object
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode != 0
    assert "object" in res.stderr.lower() or "{email" in res.stderr
