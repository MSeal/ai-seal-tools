#!/usr/bin/env python3
"""Summarize accumulated scrub feedback for rule-tuning passes.

Reads ~/.config/ai-seal-tools/voice/scrub_feedback.yaml and prints:
- Counts by reason category
- Top flagged snippets grouped by reason (e.g. "false_positive:emphasis"
  → words that are routinely allowed; consider allowlisting)
- Top flagged snippets that are routinely rejected (genuine leak patterns)

Run periodically when accumulated feedback feels actionable.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

FEEDBACK_PATH = Path.home() / ".config" / "ai-seal-tools" / "voice" / "scrub_feedback.yaml"


def main() -> int:
    if not FEEDBACK_PATH.exists():
        print(f"No feedback file at {FEEDBACK_PATH}")
        return 0
    data = yaml.safe_load(FEEDBACK_PATH.read_text()) or {}
    entries = data.get("entries", [])
    if not entries:
        print("Feedback file is empty.")
        return 0

    print(f"Total feedback entries: {len(entries)}")
    print()

    # Counts by reason
    by_reason = Counter(e.get("reason", "no_reason") for e in entries)
    print("By reason:")
    for reason, n in by_reason.most_common():
        print(f"  {reason}: {n}")
    print()

    # Snippets grouped by (reason, decision)
    snippets_by_reason: defaultdict[str, Counter] = defaultdict(Counter)
    for e in entries:
        key = f"{e.get('reason', 'no_reason')} / {e['decision']}"
        snippets_by_reason[key][e["flag_snippet"]] += 1

    print("Top flagged snippets by (reason / decision):")
    for key, counter in sorted(snippets_by_reason.items()):
        print(f"\n  [{key}]")
        for snippet, n in counter.most_common(15):
            print(f"    {snippet!r:35s}  {n}")

    # Rule-level pattern: which flag_rules are commonly false-positive vs true-positive
    print()
    print("By flag_rule × decision:")
    rule_decision: defaultdict[str, Counter] = defaultdict(Counter)
    for e in entries:
        rule_decision[e["flag_rule"]][e["decision"]] += 1
    for rule, dc in sorted(rule_decision.items()):
        total = sum(dc.values())
        accept_pct = 100 * dc.get("accept", 0) / total if total else 0
        print(f"  {rule}: total={total}, accept={dc.get('accept', 0)} ({accept_pct:.0f}%), reject={dc.get('reject', 0)}")

    # Suggest allowlist candidates: words flagged with reason=false_positive:common_word or :emphasis
    # that show up 3+ times
    print()
    print("Allowlist candidates (false_positive:common_word/emphasis, seen ≥2 times):")
    candidates: Counter = Counter()
    for e in entries:
        if e.get("reason") in ("false_positive:common_word", "false_positive:emphasis"):
            candidates[e["flag_snippet"].lower()] += 1
    for word, n in candidates.most_common():
        if n >= 2:
            print(f"  {word!r}: {n} overrides")

    return 0


if __name__ == "__main__":
    sys.exit(main())
