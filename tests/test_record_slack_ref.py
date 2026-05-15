"""Smoke tests for record_slack_ref.py — the CLI that writes handle and
channel entries. Detailed semantics (sort, dedupe, replace, lowercase)
are covered in test_slack_refs.py; here we just verify the CLI parses
flags and plumbs into the right write call."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "record_slack_ref.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_handle_subcommand_writes_entry(tmp_path):
    out = tmp_path / "s.yaml"
    result = _run(
        "--output", str(out),
        "handle",
        "--handle", "eve",
        "--email", "eve@example.com",
        "--source", "glean",
    )
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(out.read_text())
    assert data["handles"]["eve"]["email"] == "eve@example.com"
    assert data["handles"]["eve"]["source"] == "glean"


def test_handle_strips_leading_at(tmp_path):
    out = tmp_path / "s.yaml"
    _run("--output", str(out), "handle", "--handle", "@alice", "--email", "alice@x")
    data = yaml.safe_load(out.read_text())
    assert "alice" in data["handles"]
    assert "@alice" not in data["handles"]


def test_channel_subcommand_writes_entry(tmp_path):
    out = tmp_path / "s.yaml"
    result = _run(
        "--output", str(out),
        "channel",
        "--name", "dtx-eng",
        "--members", "alice@x,bob@x,carol@x",
        "--source", "glean",
        "--note", "best-effort",
    )
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(out.read_text())
    assert data["channels"]["dtx-eng"]["members"] == ["alice@x", "bob@x", "carol@x"]
    assert data["channels"]["dtx-eng"]["note"] == "best-effort"


def test_channel_strips_leading_hash(tmp_path):
    out = tmp_path / "s.yaml"
    _run("--output", str(out), "channel", "--name", "#dtx", "--members", "a@x")
    data = yaml.safe_load(out.read_text())
    assert "dtx" in data["channels"]


def test_help_works():
    result = _run("--help")
    assert result.returncode == 0
    assert "handle" in result.stdout.lower()
    assert "channel" in result.stdout.lower()
