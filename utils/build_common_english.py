#!/usr/bin/env python3
"""Generate skills/voice-analyze/data/common_english_words.txt from the
`wordfreq` library's frequency corpus.

The list is used by scrub.py to decide whether a capitalized word is
likely a proper noun (rare) vs a common English word capitalized for
structural/emphasis reasons. wordfreq provides large-corpus frequency
data backed by Google Ngrams + several other corpora.

Re-run this script when you want to refresh the list. The output is
checked into the repo so scrub.py works without runtime corpus lookups.

Usage:
    UV_NO_CONFIG=1 uv run utils/build_common_english.py [--top 5000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "skills" / "voice-analyze" / "data" / "common_english_words.txt"

DEFAULT_TOP_N = 5000
MIN_WORD_LENGTH = 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                    help=f"How many top-frequency words to include (default {DEFAULT_TOP_N}). "
                         f"Higher = more restrictive scrub (more allowed); "
                         f"lower = stricter scrub (more flagged).")
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    try:
        from wordfreq import top_n_list
    except ImportError:
        print("error: wordfreq is not installed. Run: uv sync", file=sys.stderr)
        return 1

    raw = top_n_list("en", args.top)
    # wordfreq returns lowercased words already, but normalize + filter
    words: set[str] = set()
    for w in raw:
        w = w.strip().lower()
        if not w.isalpha() or len(w) < MIN_WORD_LENGTH:
            continue
        words.add(w)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_words = sorted(words)
    out_path.write_text("\n".join(sorted_words) + "\n")
    print(f"Wrote {len(sorted_words)} words to {out_path}")
    print(f"Source: wordfreq top {args.top} English words")
    print(f"Min length: {MIN_WORD_LENGTH} chars, alphabetic only")

    # Sanity sample
    print("\nSample (every Nth):")
    step = max(1, len(sorted_words) // 20)
    for i in range(0, len(sorted_words), step):
        print(f"  {sorted_words[i]!r}")

    # Check that some specific common words we'd expect are in there
    expected = ["done", "started", "progress", "phase", "problem", "settings",
                "scope", "needed", "decisions", "section", "header"]
    missing = [w for w in expected if w not in words]
    if missing:
        print(f"\nWarning: expected common words MISSING from list: {missing}")
    else:
        print(f"\n✓ All expected common words present: {expected}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
