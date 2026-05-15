#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""record_outcome.py — append a meeting-outcome record to outcomes.jsonl.

Called by Claude inside the /find-meeting-time skill flow when the user
reports back on how an ask went ("Alice agreed to move her 1:1", "Bob
declined"). Each line is one JSON record; subsequent `find-meeting-time`
runs aggregate the log to bias future scoring (see freebusy.py
`load_outcomes` and the `learned_*` weights in score_weights.yaml).

Usage:
  record_outcome.py \\
      --attendee alice@example.com \\
      --outcome moved \\
      --event-fingerprint 'rec::abc123_R20260521T170000' \\
      [--summary "Alice / Bob 1:1"] \\
      [--note "agreed to shift by 30 min"]

The `--event-fingerprint` is the `fingerprint` field surfaced on each
conflict in freebusy.py's output. Passing it directly keeps the
attribution unambiguous across renames.

Recognized outcomes (the helper accepts anything but only these affect
scoring):
  moved       — attendee shifted the conflict to make our meeting work
  agreed      — attendee verbally agreed (same scoring effect as moved)
  declined    — attendee refused to move
  scheduled   — informational; the user picked this slot. Doesn't affect
                scoring but useful as audit history.
  skipped     — informational; the user moved on without asking. No
                scoring effect.

The file is gitignored via the Drive-symlink layout and never leaves
the local machine + the user's Drive sync.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

OUTCOMES_FILE = (
    Path.home() / ".config" / "ai-seal-tools" / "find-meeting-time" / "outcomes.jsonl"
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--attendee", required=True, help="Email of person we asked")
    p.add_argument("--outcome", required=True, help="moved|agreed|declined|scheduled|skipped|<other>")
    p.add_argument("--event-fingerprint", required=True,
                   help="The `fingerprint` value from freebusy.py's conflict output")
    p.add_argument("--summary", help="Optional human-readable event title (for audit log readability)")
    p.add_argument("--note", help="Optional free-form context")
    args = p.parse_args()

    record = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "attendee": args.attendee.strip().lower(),
        "outcome": args.outcome.strip(),
        "event_fingerprint": args.event_fingerprint,
    }
    if args.summary:
        record["summary"] = args.summary
    if args.note:
        record["note"] = args.note

    OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic append: open with O_APPEND so concurrent invocations don't
    # interleave bytes mid-line. Each json.dumps produces one line.
    fd = os.open(str(OUTCOMES_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"appended: {record}")


if __name__ == "__main__":
    main()
