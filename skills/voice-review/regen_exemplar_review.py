#!/usr/bin/env python3
"""Regenerate scratch/voice-corpus/exemplar_review.md from current pending
proposals at ~/.config/ai-seal-tools/voice/proposals/.

Run this after any of:
  - new proposals get written by propose-batch
  - scrub rules tighten/loosen and rescrub_proposals.py updates statuses
  - you want to start a fresh review pass

Existing decisions in the current review.md are NOT preserved — this is a
clean regenerate. If you've started editing decisions, finish + apply
before regenerating.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-analyze"))

from indexer import load_index  # noqa: E402

PROPOSALS_DIR = Path.home() / ".config" / "ai-seal-tools" / "voice" / "proposals"
# Review file is user-editable transient state — lives in scratch/ (gitignored)
# so source titles and synthetic exemplars don't end up in the tracked repo.
OUT_FILE = REPO_ROOT / "scratch" / "voice-corpus" / "exemplar_review.md"


def main() -> int:
    idx = load_index()
    by_hash = {s.get("source_hash"): s for s in idx["sources"] if s.get("source_hash")}

    out = []
    out.append("# Exemplar Review")
    out.append("")
    out.append("For each exemplar below, set `decision:` to `accept` or `reject`.")
    out.append("Flagged exemplars default to `reject` — change to `accept` to override.")
    out.append("")
    out.append("When overriding a default-reject (accepting a flagged exemplar), set")
    out.append("`reason:` to a category so we can analyze patterns later. Valid values:")
    out.append("")
    out.append("  false_positive:emphasis      — bold/italic/header for emphasis, not an entity")
    out.append("  false_positive:common_word   — common English word that should be allowlisted")
    out.append("  false_positive:section_label — generic section-header word, not source-specific")
    out.append("  false_positive:placeholder   — was a placeholder we asked the LLM to use")
    out.append("  false_positive:other         — false positive, see notes")
    out.append("  true_positive:override       — real leak but accepted in context")
    out.append("")
    out.append("`reason_notes:` is optional free-text to add nuance.")
    out.append("")
    out.append("Stats and descriptors will be auto-accepted (no review needed — the")
    out.append("audience/doc_type override from review.md ensures correct routing).")
    out.append("")
    out.append("When done, run:")
    out.append("")
    out.append("    UV_NO_CONFIG=1 uv run skills/voice-review/apply_exemplar_review.py")
    out.append("")
    out.append("---")

    n_proposals = 0
    n_total = 0
    n_flagged_total = 0
    for p in sorted(PROPOSALS_DIR.glob("prop_*.yaml")):
        d = yaml.safe_load(p.read_text())
        if d.get("review_status") != "pending":
            continue
        n_proposals += 1
        cls = d["classification"]
        audience = d.get("override_audience") or cls["audience"]
        doc_type = d.get("override_doc_type") or cls["doc_type"]
        src = by_hash.get(d["source_hash"])
        title = src["title"] if src else "(unknown source)"
        n_ex = len(d.get("candidate_exemplars", []))
        n_flag = sum(1 for e in d["candidate_exemplars"] if e.get("scrub_status") == "flagged")
        n_total += n_ex
        n_flagged_total += n_flag

        out.append("")
        out.append(f"## {p.name}")
        out.append("")
        out.append(f"- source: {title!r}")
        out.append(f"- audience: {audience}")
        out.append(f"- doc_type: {doc_type}")
        out.append(f"- exemplars: {n_ex} ({n_flag} flagged)")
        out.append("")

        for i, e in enumerate(d["candidate_exemplars"], 1):
            flagged = e.get("scrub_status") == "flagged"
            default = "reject" if flagged else "accept"
            out.append(f"### {i}. {e['id']}{' — FLAGGED' if flagged else ''}")
            out.append("")
            out.append(f"- pattern: {e['pattern']!r}")
            out.append(f"- synthetic: {e['synthetic']!r}")
            out.append(f"- when: {e['when_to_use']!r}")
            if flagged:
                for f in e.get("scrub_findings", []):
                    out.append(f"- flag: [{f['rule']}] {f['snippet']!r} — {f['detail']}")
            out.append(f"- decision: {default}    # accept | reject")
            if flagged:
                out.append(f"- reason:             # when accepting flagged: false_positive:emphasis | common_word | section_label | placeholder | other | true_positive:override")
                out.append(f"- reason_notes:       # optional free-text")
            out.append("")

    OUT_FILE.write_text("\n".join(out))
    print(f"Wrote {OUT_FILE}")
    print(f"Pending proposals: {n_proposals}")
    print(f"Total exemplars: {n_total} ({n_flagged_total} flagged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
