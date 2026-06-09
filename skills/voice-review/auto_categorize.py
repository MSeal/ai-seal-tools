#!/usr/bin/env python3
"""Auto-categorize rejected exemplars in a review file by inspecting their
proper-noun flags and deciding whether each token is a real name leak
(→ accept + substitute: auto) or a common-noun false positive
(→ accept + reason: false_positive:section_label).

Useful as a fast-path before hand review: it turns the "every flagged
exemplar starts as `reject`" default into a sensible starting decision
set so the reviewer only has to override the genuinely ambiguous cases.

The heuristic is `should_substitute_name` from substituter.py:
- If any flagged proper-noun is adjacent to a placeholder first name OR
  appears after an explicit people-context marker (Attendees:, By:, @)
  → flip to accept with `substitute: auto` so substituter rewrites the
  text on merge.
- Otherwise (all flags are common nouns in section/title positions)
  → flip to accept with `reason: false_positive:section_label`.

Doesn't touch already-accepted exemplars or rejected exemplars without
proper-noun flags (e.g. ngram-overlap leaks need explicit reviewer
judgment — the auto-categorizer leaves those alone).

Usage:
    UV_NO_CONFIG=1 uv run skills/voice-review/auto_categorize.py [REVIEW_FILE]

REVIEW_FILE defaults to scratch/voice-corpus/exemplar_review.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-review"))
from substituter import should_substitute_name  # noqa: E402

DEFAULT_REVIEW_FILE = REPO_ROOT / "scratch" / "voice-corpus" / "exemplar_review.md"
DEFAULT_PROPOSALS_DIR = Path.home() / ".config" / "ai-seal-tools" / "voice" / "proposals"


def load_synthetic_for_exemplar(ex_id: str, proposals_dir: Path) -> str:
    """Find an exemplar's synthetic/pattern/when_to_use text by ID across
    pending proposals. Combined so name-leak detection has full context."""
    for p in proposals_dir.glob("prop_*.yaml"):
        try:
            d = yaml.safe_load(p.read_text())
        except yaml.YAMLError:
            continue
        if d.get("review_status") != "pending":
            continue
        for ex in d.get("candidate_exemplars", []):
            if ex.get("id") == ex_id:
                return "\n".join([
                    ex.get("synthetic", ""),
                    ex.get("pattern", ""),
                    ex.get("when_to_use", ""),
                ])
    return ""


def _set_field(ex_body: str, field: str, value: str) -> str:
    """Set or insert a `- {field}:` line in an exemplar's review block.
    If the field exists, replace the value. Otherwise insert after the
    decision line so the file's visual structure stays consistent."""
    pattern = re.compile(rf"^- {field}:.*$", re.MULTILINE)
    if pattern.search(ex_body):
        return pattern.sub(f"- {field}: {value}", ex_body)
    return re.sub(
        r"(^- decision:.*$)",
        rf"\1\n- {field}: {value}",
        ex_body, count=1, flags=re.MULTILINE,
    )


def categorize_review_file(
    review_file: Path,
    proposals_dir: Path,
) -> tuple[int, int, int]:
    """Walk every exemplar block, flip rejects→accepts where the proper-noun
    flags don't warrant rejection. Writes back to `review_file`.

    Returns (n_substitute_flips, n_false_positive_flips, n_unchanged).
    """
    text = review_file.read_text()
    sections = re.split(r"^## (prop_\S+?\.yaml)\s*$", text, flags=re.MULTILINE)

    n_substitute = 0
    n_false_positive = 0
    n_unchanged = 0
    updated = [sections[0]]

    for i in range(1, len(sections) - 1, 2):
        prop_name = sections[i].strip()
        body = sections[i + 1]
        ex_blocks = re.split(r"^### \d+\. (ex_\S+?)(?: — FLAGGED)?\s*$", body, flags=re.MULTILINE)
        new_parts = [ex_blocks[0]]

        for j in range(1, len(ex_blocks) - 1, 2):
            ex_id = ex_blocks[j].strip()
            ex_body = ex_blocks[j + 1]
            decision_m = re.search(r"^- decision:\s*([^\s#]\S*)", ex_body, re.MULTILINE)
            current = decision_m.group(1).lower() if decision_m else "reject"

            if current == "reject":
                flags = re.findall(r"^- flag: \[leak:proper_noun\] (\S+?) —", ex_body, re.MULTILINE)
                if flags:
                    synthetic = load_synthetic_for_exemplar(ex_id, proposals_dir)
                    words = [f.strip("'\"") for f in flags]
                    needs_sub = any(should_substitute_name(synthetic, w) for w in words)
                    if needs_sub:
                        ex_body = re.sub(
                            r"^- decision:.*$",
                            "- decision: accept    # accept | reject",
                            ex_body, flags=re.MULTILINE,
                        )
                        ex_body = _set_field(ex_body, "reason", "true_positive:override")
                        ex_body = _set_field(ex_body, "reason_notes", "name leak auto-substituted with placeholders")
                        ex_body = _set_field(ex_body, "substitute", "auto")
                        n_substitute += 1
                    else:
                        ex_body = re.sub(
                            r"^- decision:.*$",
                            "- decision: accept    # accept | reject",
                            ex_body, flags=re.MULTILINE,
                        )
                        ex_body = _set_field(ex_body, "reason", "false_positive:section_label")
                        ex_body = _set_field(ex_body, "reason_notes", "common-noun-in-title or domain term, not source-specific")
                        n_false_positive += 1
                else:
                    n_unchanged += 1

            flagged_suffix = " — FLAGGED" if re.search(r"^- flag:", ex_body, re.MULTILINE) else ""
            new_parts.append(f"### {(j + 1) // 2}. {ex_id}{flagged_suffix}\n")
            new_parts.append(ex_body)

        updated.append(f"## {prop_name}\n")
        updated.append("".join(new_parts))

    review_file.write_text("".join(updated))
    return n_substitute, n_false_positive, n_unchanged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("review_file", nargs="?", default=str(DEFAULT_REVIEW_FILE))
    parser.add_argument("--proposals-dir", default=str(DEFAULT_PROPOSALS_DIR))
    args = parser.parse_args(argv)

    review_path = Path(args.review_file)
    if not review_path.exists():
        print(f"Review file not found: {review_path}", file=sys.stderr)
        return 1

    n_sub, n_fp, n_unch = categorize_review_file(review_path, Path(args.proposals_dir))
    print(f"Updated {review_path}")
    print(f"  flipped to accept + substitute: {n_sub}")
    print(f"  flipped to accept (false positive): {n_fp}")
    print(f"  left as-is (no proper-noun flags to auto-decide): {n_unch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
