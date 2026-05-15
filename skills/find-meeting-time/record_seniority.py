#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""record_seniority.py — write one seniority entry to seniority.yaml.

Called by Claude in the /find-meeting-time skill flow after looking up
a conflict attendee via Glean (or any other directory). Internally
constructs a SeniorityFields object, runs infer_tier, and writes the
resulting record to disk. Subsequent freebusy.py runs read the file
and apply per-conflict seniority penalties.

Usage (typical, after Glean lookup):
  record_seniority.py \\
      --email some-vp@example.com \\
      --title 'VP, Engineering' \\
      --department 'Engineering' \\
      --total-reports-count 50 \\
      --source glean

Manual override (e.g., user knows the tier directly):
  record_seniority.py \\
      --email alice@example.com \\
      --title 'Director, AI Platform' \\
      --tier 3 \\
      --source manual

Source values: 'glean', 'ldap', 'manual', 'inferred', ... — free-form,
documents where the entry came from for future audit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve sibling modules without needing a package install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seniority import SeniorityFields, infer_tier, write_record  # noqa: E402

OUTPUT_FILE = (
    Path.home() / ".config" / "ai-seal-tools" / "find-meeting-time" / "seniority.yaml"
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--email", required=True)
    p.add_argument("--title", help="Job title; drives tier inference if --tier not passed")
    p.add_argument("--department")
    p.add_argument("--manager-title")
    p.add_argument("--total-reports-count", type=int)
    p.add_argument("--source", default="manual", help="Where this data came from")
    p.add_argument("--tier", type=int, help="Override inferred tier (0-5). Omit to use infer_tier.")
    p.add_argument("--output", type=Path, default=OUTPUT_FILE, help=f"Path to seniority.yaml (default {OUTPUT_FILE})")
    args = p.parse_args()

    fields = SeniorityFields(
        email=args.email.strip().lower(),
        title=args.title,
        department=args.department,
        manager_title=args.manager_title,
        total_reports_count=args.total_reports_count,
        source=args.source,
    )
    tier = args.tier if args.tier is not None else infer_tier(fields)
    write_record(args.output, fields, tier=tier)
    print(f"wrote: {args.email.lower()} → tier {tier} (source={args.source}, title={args.title!r})")


if __name__ == "__main__":
    main()
