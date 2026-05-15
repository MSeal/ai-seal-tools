#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""record_slack_ref.py — write a resolved Slack reference (handle or
channel) into slack_refs.yaml.

Called by Claude in the /find-meeting-time skill flow after looking up
a `@handle` or `#channel` in the user's request via Glean (or any
other directory once available). Two subcommands.

Handle (after Glean app=people lookup):
  record_slack_ref.py handle \\
      --handle eve \\
      --email eve@example.com \\
      --source glean

Channel (after Glean app=slack channel search):
  record_slack_ref.py channel \\
      --name dtx-eng \\
      --members alice@example.com,bob@example.com,carol@example.com \\
      --source glean \\
      --note "Best-effort: inferred from message authors"

Source values are free-form ('glean', 'slack', 'manual', etc.) and
recorded for audit, not interpreted by the lookup path. The file is
gitignored and Drive-backed via the same symlink layout as the rest
of the per-skill config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve sibling modules without packaging.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from slack_refs import SlackChannel, SlackHandle, write_channel, write_handle  # noqa: E402

OUTPUT_FILE = (
    Path.home() / ".config" / "ai-seal-tools" / "find-meeting-time" / "slack_refs.yaml"
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, default=OUTPUT_FILE, help=f"Path to slack_refs.yaml (default {OUTPUT_FILE})")
    sub = p.add_subparsers(dest="kind", required=True)

    sp = sub.add_parser("handle", help="Record a @handle → email mapping")
    sp.add_argument("--handle", required=True, help="Slack handle (with or without leading @)")
    sp.add_argument("--email", required=True)
    sp.add_argument("--source", default="manual")

    sp = sub.add_parser("channel", help="Record a #channel → member emails mapping")
    sp.add_argument("--name", required=True, help="Channel name (with or without leading #)")
    sp.add_argument("--members", required=True, help="Comma-separated emails")
    sp.add_argument("--source", default="manual")
    sp.add_argument("--note", default="", help="Caveat about completeness/freshness")

    args = p.parse_args()

    if args.kind == "handle":
        h = SlackHandle(handle=args.handle, email=args.email, source=args.source)
        write_handle(args.output, h)
        print(f"wrote handle: @{h.handle.lstrip('@').lower()} → {h.email.lower()} (source={h.source})")
    elif args.kind == "channel":
        members = tuple(m.strip() for m in args.members.split(",") if m.strip())
        c = SlackChannel(name=args.name, member_emails=members, source=args.source, note=args.note)
        write_channel(args.output, c)
        print(f"wrote channel: #{c.name.lstrip('#').lower()} → {len(members)} members (source={c.source})")


if __name__ == "__main__":
    main()
