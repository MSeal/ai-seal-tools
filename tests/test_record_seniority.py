"""Smoke tests for record_seniority.py — the thin CLI that writes one
entry into seniority.yaml. We don't re-test infer_tier or write_record
here (covered in test_seniority.py); just confirm the CLI plumbs flags
into the right call."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "find-meeting-time" / "record_seniority.py"


def _run(output: Path, **kwargs):
    args = [sys.executable, str(SCRIPT)]
    for k, v in kwargs.items():
        args.append(f"--{k.replace('_', '-')}")
        args.append(str(v))
    args.extend(["--output", str(output)])
    return subprocess.run(args, capture_output=True, text=True)


def test_writes_entry_with_inferred_tier(tmp_path):
    """With no --tier flag, infer_tier runs on the title."""
    out = tmp_path / "seniority.yaml"
    result = _run(out, email="vp@x", title="VP, Engineering", source="manual")
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(out.read_text())
    assert data["seniority"]["vp@x"]["tier"] == 4
    assert data["seniority"]["vp@x"]["title"] == "VP, Engineering"
    assert data["seniority"]["vp@x"]["source"] == "manual"


def test_explicit_tier_overrides_inference(tmp_path):
    out = tmp_path / "seniority.yaml"
    _run(out, email="alice@x", title="Engineer", tier=5, source="manual")
    data = yaml.safe_load(out.read_text())
    assert data["seniority"]["alice@x"]["tier"] == 5


def test_glean_source_records_full_audit_fields(tmp_path):
    """A Glean-driven invocation should preserve title/department/manager-title
    in the on-disk record."""
    out = tmp_path / "seniority.yaml"
    _run(
        out,
        email="alice@x",
        title="Director II, Engineering",
        department="Engineering",
        manager_title="VP, Engineering",
        total_reports_count=42,
        source="glean",
    )
    rec = yaml.safe_load(out.read_text())["seniority"]["alice@x"]
    assert rec["tier"] == 3
    assert rec["department"] == "Engineering"
    assert rec["manager_title"] == "VP, Engineering"
    assert rec["total_reports_count"] == 42
    assert rec["source"] == "glean"


def test_help_works(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "seniority" in result.stdout.lower()
