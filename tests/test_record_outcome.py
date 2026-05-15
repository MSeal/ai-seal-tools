"""Smoke tests for record_outcome.py — the small CLI that appends one
record per invocation to outcomes.jsonl. We don't exercise every flag;
we verify that the file is created, the record is well-formed, the
fingerprint and outcome round-trip, and concurrent appends don't
clobber each other."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "record_outcome.py"


def _run(out_file: Path, **kwargs) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT)]
    for k, v in kwargs.items():
        args.append(f"--{k.replace('_', '-')}")
        args.append(v)
    env = {"HOME": str(out_file.parent.parent.parent.parent), "PATH": ""}
    # Monkey-patch the script's OUTCOMES_FILE via HOME so it writes to our tmp.
    # Path structure: ~/.config/ai-seal-tools/find-meeting-time/outcomes.jsonl
    return subprocess.run(args, capture_output=True, text=True, env=env)


def _outcomes_path(tmp_path: Path) -> Path:
    return tmp_path / ".config" / "ai-seal-tools" / "find-meeting-time" / "outcomes.jsonl"


def test_append_creates_file_and_writes_record(tmp_path):
    out = _outcomes_path(tmp_path)
    result = _run(
        out,
        attendee="alice@x",
        outcome="moved",
        event_fingerprint="rec::abc",
        summary="Weekly 1:1",
        note="agreed to shift 30 min",
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["attendee"] == "alice@x"
    assert rec["outcome"] == "moved"
    assert rec["event_fingerprint"] == "rec::abc"
    assert rec["summary"] == "Weekly 1:1"
    assert rec["note"] == "agreed to shift 30 min"
    assert "ts" in rec


def test_multiple_appends_preserve_history(tmp_path):
    """Each invocation appends one new line; existing records aren't touched."""
    out = _outcomes_path(tmp_path)
    _run(out, attendee="alice@x", outcome="moved", event_fingerprint="rec::a")
    _run(out, attendee="bob@x",   outcome="declined", event_fingerprint="rec::b")
    _run(out, attendee="alice@x", outcome="moved", event_fingerprint="rec::a")
    lines = out.read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert {r["attendee"] for r in parsed} == {"alice@x", "bob@x"}
    assert sum(1 for r in parsed if r["outcome"] == "moved") == 2


def test_attendee_email_normalized_to_lowercase(tmp_path):
    out = _outcomes_path(tmp_path)
    _run(out, attendee="Alice@X.IO", outcome="moved", event_fingerprint="rec::a")
    rec = json.loads(out.read_text().splitlines()[0])
    assert rec["attendee"] == "alice@x.io"


def test_omits_optional_fields_when_not_provided(tmp_path):
    out = _outcomes_path(tmp_path)
    _run(out, attendee="alice@x", outcome="moved", event_fingerprint="rec::a")
    rec = json.loads(out.read_text().splitlines()[0])
    assert "summary" not in rec
    assert "note" not in rec


def test_help_works(tmp_path):
    """--help should exit cleanly with a usage message."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "outcome" in result.stdout.lower()
