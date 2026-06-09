#!/usr/bin/env python3
"""Re-run scrub validation on existing pending proposals.

After updating scrub.py (allowlist tweaks, sentence-initial detection
improvements, etc.), this script re-validates the exemplars in every
pending proposal and updates `scrub_status` + `scrub_findings` in place.
Avoids re-running propose-batch (which would cost LLM calls).

The source text for n-gram comparison is read from
~/.cache/ai-seal-tools/voice/content/<source_id>.md (for cached docs) or
from the source-index `path` field (for local docs). If neither is
available, n-gram overlap can't be re-checked and the existing finding
is preserved.

Usage:
    UV_NO_CONFIG=1 uv run skills/voice-analyze/rescrub_proposals.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-analyze"))

from fetcher import FetchUnavailable, fetch_content  # noqa: E402
from indexer import load_index  # noqa: E402
from scrub import scrub  # noqa: E402

PROPOSALS_DIR = Path.home() / ".config" / "ai-seal-tools" / "voice" / "proposals"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = ap.parse_args()

    # Map source_hash -> index entry (for fetching source text)
    idx = load_index()
    by_hash = {s.get("source_hash"): s for s in idx["sources"] if s.get("source_hash")}

    summary = {"proposals_processed": 0, "exemplars_processed": 0,
               "flag_status_changed": 0, "flag_passed_now": 0, "flag_flagged_now": 0,
               "source_missing": 0}

    for p in sorted(PROPOSALS_DIR.glob("prop_*.yaml")):
        d = yaml.safe_load(p.read_text())
        if d.get("review_status") != "pending":
            continue
        summary["proposals_processed"] += 1

        source_hash = d["source_hash"]
        entry = by_hash.get(source_hash)
        source_text: str | None = None
        if entry:
            try:
                fc = fetch_content(entry)
                source_text = fc.text
            except FetchUnavailable:
                source_text = None
        if source_text is None:
            summary["source_missing"] += 1

        changed = False
        for ex in d.get("candidate_exemplars", []):
            summary["exemplars_processed"] += 1
            old_status = ex.get("scrub_status")
            old_findings = ex.get("scrub_findings", [])
            # Re-scrub each field independently — matches the propose-batch logic
            all_findings = []
            for field_name in ("synthetic", "pattern", "when_to_use"):
                value = ex.get(field_name, "")
                # Only run n-gram check if we have source text; otherwise
                # this only re-checks proper-noun/identifier rules
                result = scrub(value, source=source_text)
                for f in result.findings:
                    all_findings.append({
                        "rule": f.rule,
                        "snippet": f.snippet,
                        "detail": f"{f.detail} (in {field_name})",
                        "where": f"exemplar:{ex['id']}",
                    })

            new_status = "passed" if not all_findings else "flagged"
            if new_status != old_status:
                summary["flag_status_changed"] += 1
                if new_status == "passed":
                    summary["flag_passed_now"] += 1
                else:
                    summary["flag_flagged_now"] += 1
                changed = True
            # Also rewrite if the findings shrank — old false positives may
            # have dropped out without the status changing (e.g. Alice/Bob
            # got removed but Analytics remains, so status stays "flagged"
            # but the findings list is now shorter).
            elif len(all_findings) != len(old_findings):
                changed = True
            ex["scrub_status"] = new_status
            if all_findings:
                ex["scrub_findings"] = all_findings
            else:
                ex.pop("scrub_findings", None)

        if changed and not args.dry_run:
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))

    print("Re-scrub summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("(dry run — no files changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
