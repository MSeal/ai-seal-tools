#!/usr/bin/env python3
"""Apply exemplar review decisions from `exemplar_review.md` to all pending
proposals atomically.

For each proposal in ~/.config/ai-seal-tools/voice/proposals/:
  - Auto-accept stats (merge into bucket via weighted average)
  - Auto-accept all descriptor fields (dedup-append for lists; replace for prose)
  - Apply per-exemplar accept/reject decisions from the review file
  - Update profile.yaml, sources_seen.yaml, archive the proposal

If a proposal isn't covered in the review file (or has zero accept decisions),
the script still merges its stats+descriptors but skips all its exemplars.

Usage:
    UV_NO_CONFIG=1 uv run skills/voice-review/apply_exemplar_review.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-analyze"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-review"))

from reviewer import (  # noqa: E402
    apply_proposal_to_profile,
    load_profile,
    load_sources_seen,
    save_profile,
    save_sources_seen,
    update_sources_seen,
    ARCHIVE_DIR,
    PROPOSALS_DIR,
)

REVIEW_FILE = REPO_ROOT / "scratch" / "voice-corpus" / "exemplar_review.md"
FEEDBACK_PATH = Path.home() / ".config" / "ai-seal-tools" / "voice" / "scrub_feedback.yaml"

VALID_REASONS = {
    "false_positive:emphasis",
    "false_positive:common_word",
    "false_positive:section_label",
    "false_positive:placeholder",
    "false_positive:other",
    "true_positive:override",
    "no_reason",
}

SURROUNDING_CHARS = 50


# Parse the review file. Each "## prop_XYZ.yaml" section contains exemplar
# blocks with `decision:`, optional `reason:`, optional `reason_notes:`.
# Returns: {proposal_name: {exemplar_id: {decision, reason, reason_notes}}}
def parse_review_file(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    text = path.read_text()
    out: dict[str, dict[str, dict[str, str]]] = {}
    sections = re.split(r"^## (prop_\S+?\.yaml)\s*$", text, flags=re.MULTILINE)
    for i in range(1, len(sections) - 1, 2):
        proposal_name = sections[i].strip()
        body = sections[i + 1]
        exemplars: dict[str, dict[str, str]] = {}
        ex_blocks = re.split(r"^### \d+\. (ex_\S+?)(?: — FLAGGED)?\s*$", body, flags=re.MULTILINE)
        for j in range(1, len(ex_blocks) - 1, 2):
            ex_id = ex_blocks[j].strip()
            ex_body = ex_blocks[j + 1]
            # Require a non-comment first character so empty fields whose
            # only text is an inline comment (e.g. "- reason:    # ...") don't
            # capture the comment as the value.
            decision_m = re.search(r"^- decision:\s*([^\s#]\S*)", ex_body, re.MULTILINE)
            reason_m = re.search(r"^- reason:\s*([^\s#]\S*)", ex_body, re.MULTILINE)
            notes_m = re.search(r"^- reason_notes:\s*([^\s#].*?)(?:\s*#|$)", ex_body, re.MULTILINE)
            if not decision_m:
                continue
            entry = {"decision": decision_m.group(1).strip().lower()}
            if reason_m:
                entry["reason"] = reason_m.group(1).strip()
            if notes_m:
                entry["reason_notes"] = notes_m.group(1).strip()
            exemplars[ex_id] = entry
        out[proposal_name] = exemplars
    return out


def _surrounding_window(text: str, snippet: str) -> str:
    """Extract ~50 chars on each side of `snippet` from `text`."""
    idx = text.find(snippet)
    if idx < 0:
        return ""
    start = max(0, idx - SURROUNDING_CHARS)
    end = min(len(text), idx + len(snippet) + SURROUNDING_CHARS)
    return text[start:end]


def load_feedback() -> dict:
    if not FEEDBACK_PATH.exists():
        return {"schema_version": 1, "entries": []}
    return yaml.safe_load(FEEDBACK_PATH.read_text()) or {"schema_version": 1, "entries": []}


def save_feedback(data: dict, dry_run: bool) -> None:
    if dry_run:
        return
    from datetime import datetime, timezone
    data["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if "generated_at" not in data or data.get("generated_at", "").startswith("1970"):
        data["generated_at"] = data["last_updated"]
    # Validate before write
    sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-analyze"))
    import schema as voice_schema
    voice_schema.validate("scrub_feedback", data)
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PATH.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def record_feedback(feedback: dict, *, proposal_id: str, exemplar: dict,
                    source_hash: str, decision: str, reason: str | None,
                    reason_notes: str | None) -> int:
    """Append one feedback entry per scrub finding on the exemplar.
    Returns number of entries added."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    added = 0
    for finding in exemplar.get("scrub_findings", []):
        # Decide which field the flag was in (parsed from detail string,
        # which the scrub writes as e.g. "... (in synthetic)")
        field_match = re.search(r"\(in (synthetic|pattern|when_to_use)\)$", finding.get("detail", ""))
        field = field_match.group(1) if field_match else "unknown"
        text_for_window = exemplar.get(field, "") if field != "unknown" else ""
        entry = {
            "logged_at": now,
            "proposal_id": proposal_id,
            "exemplar_id": exemplar["id"],
            "source_hash": source_hash,
            "flag_rule": finding["rule"],
            "flag_snippet": finding["snippet"],
            "flag_detail": finding.get("detail", ""),
            "field": field,
            "surrounding_text": _surrounding_window(text_for_window, finding["snippet"]),
            "decision": decision,
            "reason": reason or "no_reason",
        }
        if reason_notes:
            entry["reason_notes"] = reason_notes
        feedback["entries"].append(entry)
        added += 1
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    ap.add_argument("--review-file", default=str(REVIEW_FILE))
    args = ap.parse_args()

    review_path = Path(args.review_file)
    if not review_path.is_file():
        print(f"error: review file not found: {review_path}", file=sys.stderr)
        return 1

    decisions_by_prop = parse_review_file(review_path)
    if not decisions_by_prop:
        print(f"error: parsed 0 proposal sections from {review_path}", file=sys.stderr)
        return 1
    print(f"Parsed {len(decisions_by_prop)} proposal sections.")

    profile = load_profile()
    sources_seen = load_sources_seen()
    feedback = load_feedback()

    n_merged = 0
    n_skipped_unknown = 0
    total_accepted_ex = 0
    total_rejected_ex = 0
    total_feedback_entries = 0
    invalid_reasons: list[tuple[str, str]] = []

    for prop_path in sorted(PROPOSALS_DIR.glob("prop_*.yaml")):
        d = yaml.safe_load(prop_path.read_text())
        if d.get("review_status") != "pending":
            continue
        prop_name = prop_path.name
        if prop_name not in decisions_by_prop:
            print(f"  ! no decisions found for {prop_name}, skipping")
            n_skipped_unknown += 1
            continue

        cls = d["classification"]
        audience = d.get("override_audience") or cls["audience"]
        doc_type = d.get("override_doc_type") or cls["doc_type"]
        proposal_id = d["proposal_id"]
        source_hash = d["source_hash"]

        ex_decisions = decisions_by_prop[prop_name]
        accepted_ids: set[str] = set()
        ex_notes: dict[str, str] = {}
        for ex in d.get("candidate_exemplars", []):
            entry = ex_decisions.get(ex["id"], {"decision": "reject"})
            decision = entry.get("decision", "reject")
            reason = entry.get("reason")
            reason_notes = entry.get("reason_notes")

            if reason and reason not in VALID_REASONS:
                invalid_reasons.append((ex["id"], reason))
                reason = None

            if decision == "accept":
                accepted_ids.add(ex["id"])
                if ex.get("scrub_status") == "flagged":
                    note_parts = ["accepted despite scrub flag"]
                    if reason:
                        note_parts.append(f"reason: {reason}")
                    if reason_notes:
                        note_parts.append(f"notes: {reason_notes}")
                    ex_notes[ex["id"]] = "; ".join(note_parts)

            # Log feedback for any flagged exemplar (accept or reject, with or
            # without reason) so we accumulate scrub-tuning data.
            if ex.get("scrub_status") == "flagged":
                total_feedback_entries += record_feedback(
                    feedback,
                    proposal_id=proposal_id,
                    exemplar=ex,
                    source_hash=source_hash,
                    decision=decision,
                    reason=reason,
                    reason_notes=reason_notes,
                )

        total_accepted_ex += len(accepted_ids)
        total_rejected_ex += len(d.get("candidate_exemplars", [])) - len(accepted_ids)

        # Auto-accept stats + all descriptor fields
        decisions = {
            "audience": audience,
            "doc_type": doc_type,
            "merge_stats": True,
            "merge_descriptors": True,
            "accepted_descriptor_fields": set((d.get("proposed_descriptors") or {}).keys()),
            "accepted_exemplar_ids": accepted_ids,
            "exemplar_notes": ex_notes,
        }

        profile = apply_proposal_to_profile(profile, d, decisions)
        sources_seen = update_sources_seen(
            sources_seen,
            d,
            audience=audience,
            doc_type=doc_type,
            contributed_exemplar_ids=list(accepted_ids),
        )

        d["review_status"] = "accepted"
        if not args.dry_run:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            (ARCHIVE_DIR / prop_name).write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
            prop_path.unlink()
        print(f"  merged {prop_name}: {audience}/{doc_type}, exemplars accepted={len(accepted_ids)}")
        n_merged += 1

    print()
    print(f"Summary: {n_merged} merged, {n_skipped_unknown} skipped (no decisions found)")
    print(f"Exemplars: {total_accepted_ex} accepted, {total_rejected_ex} rejected")
    print(f"Feedback entries appended: {total_feedback_entries}")
    if invalid_reasons:
        print(f"\nWarning: {len(invalid_reasons)} invalid reason values ignored:")
        for eid, r in invalid_reasons[:5]:
            print(f"  {eid}: {r!r}")

    if args.dry_run:
        print("(dry run — no files changed)")
        return 0

    save_profile(profile)
    save_sources_seen(sources_seen)
    save_feedback(feedback, dry_run=False)
    print(f"Profile saved: {profile.get('last_updated')}")
    print(f"Feedback saved: {FEEDBACK_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
