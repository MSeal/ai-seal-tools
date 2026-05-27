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
# merge_person — location / timezone
# ---------------------------------------------------------------------------


def test_merge_person_stores_location_and_infers_tz():
    """Location alone is enough — timezone falls out of the location map."""
    data: dict = {"people": {}}
    changed = rpn.merge_person(
        data, "alice@example.com", "Alice",
        location="GB Remote United Kingdom",
        source="glean", fetched_at="2026-05-27T00:00:00+00:00",
    )
    assert changed is True
    entry = data["people"]["alice@example.com"]
    assert entry["location"] == "GB Remote United Kingdom"
    assert entry["timezone"] == "Europe/London"


def test_merge_person_explicit_tz_overrides_inference():
    """When the caller passes a timezone, that beats whatever the map
    would have inferred from the location string."""
    data: dict = {"people": {}}
    rpn.merge_person(
        data, "alice@example.com", "Alice",
        location="US Remote Washington",  # would infer PT
        timezone="America/New_York",      # caller says DC
        source="glean", fetched_at="2026-05-27T00:00:00+00:00",
    )
    assert data["people"]["alice@example.com"]["timezone"] == "America/New_York"


def test_merge_person_tz_only_without_location():
    """Skill might know the TZ from a non-Glean source and want to
    record it without a `location` string."""
    data: dict = {"people": {}}
    rpn.merge_person(
        data, "alice@example.com", "Alice",
        timezone="Europe/Berlin",
        source="manual", fetched_at="2026-05-27T00:00:00+00:00",
    )
    entry = data["people"]["alice@example.com"]
    assert entry["timezone"] == "Europe/Berlin"
    assert "location" not in entry


def test_merge_person_location_only_when_name_already_cached():
    """Common path: Claude already cached the name, now backfilling TZ.
    Name passing as None mustn't clobber the existing one."""
    data: dict = {"people": {"alice@example.com": {"name": "Alice", "sources": ["glean"]}}}
    rpn.merge_person(
        data, "alice@example.com", None,
        location="US Remote California",
        source="glean", fetched_at="2026-05-27T00:00:00+00:00",
    )
    entry = data["people"]["alice@example.com"]
    assert entry["name"] == "Alice"
    assert entry["location"] == "US Remote California"
    assert entry["timezone"] == "America/Los_Angeles"


def test_merge_person_ambiguous_location_records_location_but_not_tz():
    """A US/CA location with no state/province is recorded as the raw
    string but produces no TZ entry — the user can fix manually."""
    data: dict = {"people": {}}
    rpn.merge_person(
        data, "alice@example.com", "Alice",
        location="US Remote Foo",
        source="glean", fetched_at="2026-05-27T00:00:00+00:00",
    )
    entry = data["people"]["alice@example.com"]
    assert entry["location"] == "US Remote Foo"
    assert "timezone" not in entry


def test_merge_person_idempotent_on_replay():
    """Replaying the same Glean payload produces no change the second time."""
    data: dict = {"people": {}}
    rpn.merge_person(
        data, "alice@example.com", "Alice",
        location="GB Remote United Kingdom",
        source="glean", fetched_at="2026-05-27T00:00:00+00:00",
    )
    changed = rpn.merge_person(
        data, "alice@example.com", "Alice",
        location="GB Remote United Kingdom",
        source="glean", fetched_at="2026-05-28T00:00:00+00:00",
    )
    assert changed is False


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


# ---------------------------------------------------------------------------
# CLI smoke tests for the new location/timezone flow
# ---------------------------------------------------------------------------


def test_cli_single_mode_with_location_infers_tz(tmp_path):
    p = tmp_path / "people.yaml"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p),
         "single", "--email", "jp@example.com",
         "--name", "JP Example",
         "--location", "GB Remote United Kingdom"],
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    entry = yaml.safe_load(p.read_text())["people"]["jp@example.com"]
    assert entry["name"] == "JP Example"
    assert entry["location"] == "GB Remote United Kingdom"
    assert entry["timezone"] == "Europe/London"


def test_cli_single_mode_explicit_timezone_wins(tmp_path):
    p = tmp_path / "people.yaml"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p),
         "single", "--email", "alice@example.com",
         "--name", "Alice",
         "--location", "US Remote Washington",
         "--timezone", "America/New_York"],
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    entry = yaml.safe_load(p.read_text())["people"]["alice@example.com"]
    assert entry["timezone"] == "America/New_York"


def test_cli_single_requires_at_least_one_field(tmp_path):
    """Without --name, --location, or --timezone there's nothing to write."""
    p = tmp_path / "people.yaml"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p),
         "single", "--email", "alice@example.com"],
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode != 0
    assert "name" in res.stderr or "location" in res.stderr


def test_cli_bulk_mode_mixed_legacy_and_rich_shapes(tmp_path):
    """Backward-compat: old name-only string values still work; new
    dict-shaped values can coexist in the same payload."""
    p = tmp_path / "people.yaml"
    payload = {
        "alice@example.com": "Alice Example",  # legacy name-only
        "bob@example.com": {                    # rich record
            "name": "Bob Example",
            "location": "US Remote Colorado",
        },
        "carol@example.com": {                  # tz-only, no location
            "timezone": "Europe/Paris",
        },
    }
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(p), "bulk"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        env={"UV_NO_CONFIG": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert res.returncode == 0, res.stderr
    on_disk = yaml.safe_load(p.read_text())["people"]
    assert on_disk["alice@example.com"]["name"] == "Alice Example"
    assert "location" not in on_disk["alice@example.com"]
    assert on_disk["bob@example.com"]["location"] == "US Remote Colorado"
    assert on_disk["bob@example.com"]["timezone"] == "America/Denver"
    assert on_disk["carol@example.com"]["timezone"] == "Europe/Paris"
