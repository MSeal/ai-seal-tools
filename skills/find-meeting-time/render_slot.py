#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""render_slot.py — render a freebusy slot (or a list of them) as a
markdown card with an ASCII timeline showing conflict layout.

Every card has the same shape regardless of complexity, so the user's
eye scans the same structure for all-free slots and busy-conflict ones.
Glyphs encode movability so 'don't try' vs. 'easy ask' is visible at a
glance without reading text.

Glyphs (5 chars wide each):
  ─────  free
  ░░░░░  visible conflict, easy (movability 7–10)
  ▓▓▓▓▓  visible conflict, moderate (4–6)
  █████  visible conflict, fixed (0–3)   ⚠
  ?????  opaque (free/busy only, no title)

Time labels are 12-hour without AM/PM (slot header carries the date+
suffix context). Ticks are every 15 minutes. The requester's row is
labeled '(you)'. If more than 6 attendees have conflicts the timeline
is suppressed and replaced with a "too many conflicts to visualize"
note — at that point it's a coordination problem, not a scheduling one.

Usage:
  # Single slot via stdin
  echo '<slot-json>' | render_slot.py \\
      --attendees you@example.com,alice@example.com \\
      --requester you@example.com --rank 1

  # Full freebusy.py output, render the top N slots
  render_slot.py --from freebusy.json \\
      --attendees you@example.com,alice@example.com \\
      --requester you@example.com --top 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

# Glyph chars: 5 of each per tick column.
GLYPH_FREE = "─"
GLYPH_EASY = "░"          # movability 7+
GLYPH_MODERATE = "▓"      # movability 4-6
GLYPH_FIXED = "█"         # movability 0-3
GLYPH_OPAQUE = "?"        # visible=False (free/busy only)

TICK_WIDTH = 5
COL_GAP = 2
ROW_LABEL_MIN_WIDTH = 12   # never narrower than this, keeps headers readable
ROW_LABEL_MAX_WIDTH = 26   # truncate beyond this, keeps overall width bounded
DEFAULT_TICK_MINUTES = 15
DEFAULT_MAX_VISIBLE_ATTENDEES = 6


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests in tests/test_render_slot.py)
# ---------------------------------------------------------------------------


def glyph_for_movability(movability: int, visible: bool) -> str:
    """Pick the glyph char for a single conflict based on its
    movability and visibility. Opaque (visible=False) always wins
    over movability because the conflict's true severity is unknown."""
    if not visible:
        return GLYPH_OPAQUE
    if movability >= 7:
        return GLYPH_EASY
    if movability >= 4:
        return GLYPH_MODERATE
    return GLYPH_FIXED


def _parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def conflicts_overlapping(
    tick_start: dt.datetime,
    tick_end: dt.datetime,
    conflicts: list[dict],
) -> list[dict]:
    """Return conflicts whose [conflict_start, conflict_end) overlaps
    the half-open interval [tick_start, tick_end). Any non-zero
    overlap counts — a 5-minute partial overlap shades the whole tick."""
    out: list[dict] = []
    for c in conflicts:
        cs = _parse_iso(c["conflict_start"])
        ce = _parse_iso(c["conflict_end"])
        if cs < tick_end and ce > tick_start:
            out.append(c)
    return out


def tick_glyph(
    tick_start: dt.datetime,
    tick_end: dt.datetime,
    conflicts: list[dict],
) -> str:
    """Render the single-char glyph for one tick interval. When
    multiple conflicts overlap, the worst (lowest movability, with
    opaque counted as 'unknown == worst-of-known-class') wins."""
    overlapping = conflicts_overlapping(tick_start, tick_end, conflicts)
    if not overlapping:
        return GLYPH_FREE
    # Opaque counted as "unknown" — render as ? even if some are visible,
    # because we can't combine "unknown" with "visible-movable" honestly.
    if any(not c.get("visible", True) for c in overlapping):
        return GLYPH_OPAQUE
    worst = min(overlapping, key=lambda c: c.get("movability", 5))
    return glyph_for_movability(worst.get("movability", 5), True)


def compute_boundaries(
    start: dt.datetime,
    end: dt.datetime,
    tick_minutes: int = DEFAULT_TICK_MINUTES,
) -> list[dt.datetime]:
    """Return the tick boundary times from start through end inclusive.
    For a 45-min slot at 14:30 with 15-min ticks, returns
    [14:30, 14:45, 15:00, 15:15] — 3 intervals, 4 boundaries."""
    duration = (end - start).total_seconds() / 60.0
    n_intervals = max(1, int(duration // tick_minutes))
    # If duration doesn't divide evenly (rare for 15-min ticks on 15-min
    # snapped slots), round up so the last tick covers the trailing partial.
    if duration > n_intervals * tick_minutes:
        n_intervals += 1
    bs = [start + dt.timedelta(minutes=i * tick_minutes) for i in range(n_intervals + 1)]
    # Clip the last boundary to the actual slot end so labels don't
    # overshoot for partial-tick durations.
    if bs[-1] > end:
        bs[-1] = end
    return bs


def short_time(d: dt.datetime) -> str:
    """12-hour h:mm without AM/PM. Right-aligned to TICK_WIDTH so
    single-digit-hour and double-digit-hour labels line up."""
    return d.strftime("%-I:%M").rjust(TICK_WIDTH)


def display_name_for(email: str, names: dict[str, str] | None) -> str:
    """Look up an attendee's display name from the supplied mapping (case-
    insensitive on email). Falls back to the email's local part — short
    handles are still better than blank when no name is available."""
    if names:
        key = email.strip().lower()
        for e, name in names.items():
            if e.strip().lower() == key and name.strip():
                return name.strip()
    return email.split("@")[0]


def row_label(
    email: str,
    requester_email: str | None,
    *,
    names: dict[str, str] | None = None,
    width: int | None = None,
) -> str:
    """Format the attendee row label. The requester gets a '(you)'
    suffix so the user can find their own row instantly. `names` maps
    email→full name; missing entries fall back to email local-part."""
    display = display_name_for(email, names)
    if requester_email and email.strip().lower() == requester_email.strip().lower():
        label = f"{display} (you)"
    else:
        label = display
    w = width if width is not None else ROW_LABEL_MIN_WIDTH
    if len(label) > w:
        label = label[: w - 1] + "…"
    return label.ljust(w)


def _compute_row_label_width(
    emails: list[str],
    requester_email: str | None,
    names: dict[str, str] | None,
) -> int:
    """Pick a label width that fits the longest display name for this
    render. Clamped to [ROW_LABEL_MIN_WIDTH, ROW_LABEL_MAX_WIDTH] so
    very short attendee lists still look like proper columns and very
    long names don't make the timeline disappear off the right."""
    longest = 0
    for e in emails:
        display = display_name_for(e, names)
        if requester_email and e.strip().lower() == requester_email.strip().lower():
            display = f"{display} (you)"
        longest = max(longest, len(display))
    return max(ROW_LABEL_MIN_WIDTH, min(longest, ROW_LABEL_MAX_WIDTH))


def conflict_annotation(
    conflicts: list[dict],
    *,
    is_requester: bool = False,
) -> str:
    """Build the inline annotation that follows the timeline row when
    an attendee has conflicts. Shows the worst conflict by movability
    plus a count if multiple."""
    if not conflicts:
        return ""
    visible = [c for c in conflicts if c.get("visible", True)]
    opaque_count = sum(1 for c in conflicts if not c.get("visible", True))

    if not visible:
        return f"opaque ({opaque_count} conflict{'s' if opaque_count != 1 else ''})"

    worst = min(visible, key=lambda c: c.get("movability", 5))
    summary = worst.get("summary", "(no title)")
    m = worst.get("movability", 5)
    if m >= 7:
        tag = f"movability {m}"
    elif m >= 4:
        tag = f"moderate; {m}"
    else:
        tag = f"⚠ fixed; {m}"

    extra = ""
    n_others = len(visible) - 1 + opaque_count
    if n_others > 0:
        extra = f" (+{n_others} more)"
    self_marker = " ← you" if is_requester else ""
    return f'"{summary}" ({tag}){extra}{self_marker}'


def render_timeline(
    start: dt.datetime,
    end: dt.datetime,
    attendees_conflicts: list[tuple[str, list[dict]]],
    requester_email: str | None,
    *,
    names: dict[str, str] | None = None,
    tick_minutes: int = DEFAULT_TICK_MINUTES,
) -> str:
    """Build the multi-line timeline block. `attendees_conflicts` is an
    ordered list of (email, conflicts) — free attendees pass empty list.
    `names` is an optional email→display-name map; missing entries
    fall back to the email local-part."""
    boundaries = compute_boundaries(start, end, tick_minutes)
    label_width = _compute_row_label_width(
        [e for e, _ in attendees_conflicts], requester_email, names,
    )

    # Header: row-label-pad + N+1 time labels separated by COL_GAP spaces.
    pad = " " * (label_width + COL_GAP)
    label_parts = [pad]
    for b in boundaries:
        label_parts.append(short_time(b))
        label_parts.append(" " * COL_GAP)
    header = "".join(label_parts).rstrip()

    # Body rows. Each attendee row gets N glyphs (one per interval),
    # followed by the inline annotation when there's a conflict.
    n_intervals = len(boundaries) - 1
    rows: list[str] = []
    for email, conflicts in attendees_conflicts:
        parts = [row_label(email, requester_email, names=names, width=label_width) + (" " * COL_GAP)]
        for i in range(n_intervals):
            g = tick_glyph(boundaries[i], boundaries[i + 1], conflicts) * TICK_WIDTH
            parts.append(g)
            parts.append(" " * COL_GAP)
        row = "".join(parts).rstrip()
        if conflicts:
            is_self = bool(requester_email and email.strip().lower() == requester_email.strip().lower())
            row += "  " + conflict_annotation(conflicts, is_requester=is_self)
        rows.append(row)
    return header + "\n" + "\n".join(rows)


def slot_summary_line(
    slot: dict,
    by_attendee: dict[str, list[dict]],
) -> str:
    """One-line state of the slot for the header row. 'all free' /
    'N movable' / '⚠ N conflicts — F fixed, M movable' / etc."""
    flat = [c for cs in by_attendee.values() for c in cs]
    if not flat:
        return "all free"
    fixed = sum(1 for c in flat if c.get("visible", True) and c.get("movability", 5) <= 3)
    movable = sum(1 for c in flat if c.get("visible", True) and c.get("movability", 5) >= 7)
    opaque = sum(1 for c in flat if not c.get("visible", True))
    n = len(flat)
    moderate = n - fixed - movable - opaque
    bits = []
    if fixed:
        bits.append(f"{fixed} fixed")
    if moderate:
        bits.append(f"{moderate} moderate")
    if movable:
        bits.append(f"{movable} movable")
    if opaque:
        bits.append(f"{opaque} opaque")
    detail = ", ".join(bits)
    if fixed > 0:
        return f"⚠ {n} conflict{'s' if n != 1 else ''} — {detail}"
    return f"{n} conflict{'s' if n != 1 else ''} — {detail}"


def short_date(d: dt.datetime) -> str:
    """Wed, May 22 — no year (slot range context covers it)."""
    return d.strftime("%a, %b %-d")


def format_slot_card(
    slot: dict,
    *,
    attendees: list[str],
    requester_email: str | None = None,
    rank: int | None = None,
    tick_minutes: int = DEFAULT_TICK_MINUTES,
    max_visible_attendees: int = DEFAULT_MAX_VISIBLE_ATTENDEES,
    names: dict[str, str] | None = None,
) -> str:
    """Render a single slot as a markdown block: header line + fenced
    code block with the timeline. Returns the full multi-line string."""
    start = _parse_iso(slot["start"])
    end = _parse_iso(slot["end"])
    duration_min = int(round((end - start).total_seconds() / 60.0))
    score = slot.get("score", 0)

    # Group conflicts by attendee, preserving the input attendee order.
    by_attendee: dict[str, list[dict]] = {e.lower(): [] for e in attendees}
    extras: dict[str, list[dict]] = {}
    for entry in slot.get("conflicts", []) or []:
        att = (entry.get("attendee") or "").lower()
        conflict = entry.get("conflict")
        if not att or not conflict:
            continue
        if att in by_attendee:
            by_attendee[att].append(conflict)
        else:
            extras.setdefault(att, []).append(conflict)

    # Header line.
    rank_prefix = f"{rank}. " if rank is not None else ""
    summary = slot_summary_line(slot, {**by_attendee, **extras})
    header = (
        f"{rank_prefix}**{short_date(start)} · {start.strftime('%-I:%M')}–"
        f"{end.strftime('%-I:%M %p')}** ({duration_min} min) — "
        f"Score {score} · {summary}"
    )

    # Decide whether to draw the timeline or fall back to a list.
    conflicted_count = sum(1 for cs in by_attendee.values() if cs) + sum(1 for cs in extras.values() if cs)
    if conflicted_count > max_visible_attendees:
        body_lines = [
            f"\n> {conflicted_count} attendees have conflicts — too many to visualize. "
            "This is a coordination problem, not a scheduling one. Consider trimming "
            "the attendee list, splitting the meeting, or asking for a wider time window.",
            "",
        ]
        # Show a flat list so the user can still see who's busy.
        for att in list(by_attendee.keys()) + list(extras.keys()):
            cs = by_attendee.get(att) or extras.get(att) or []
            if not cs:
                continue
            line = "- " + display_name_for(att, names)
            if requester_email and att == requester_email.strip().lower():
                line += " (you)"
            line += ": " + conflict_annotation(cs)
            body_lines.append(line)
        return header + "\n".join(body_lines)

    # Build the per-row list in display order: input attendees first
    # (preserves caller's ordering), then extras (anyone the helper
    # surfaced as conflicting but who wasn't in the original list).
    attendees_conflicts: list[tuple[str, list[dict]]] = []
    for e in attendees:
        attendees_conflicts.append((e, by_attendee.get(e.lower(), [])))
    for e in extras:
        attendees_conflicts.append((e, extras[e]))

    timeline = render_timeline(
        start, end, attendees_conflicts, requester_email,
        names=names, tick_minutes=tick_minutes,
    )
    return header + "\n```\n" + timeline + "\n```"


def parse_names_arg(raw: str) -> dict[str, str]:
    """Parse the --names CLI arg ('email=Name,email=Name') into a dict.
    Empty input → empty dict. Whitespace-only entries skipped. Names
    are trimmed; missing '=' on an entry is silently dropped (rather
    than failing the whole render — caller can re-check by inspecting
    the result)."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        email, _, name = chunk.partition("=")
        email = email.strip().lower()
        name = name.strip()
        if email and name:
            out[email] = name
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--attendees", required=True, help="Comma-separated attendee emails in display order")
    p.add_argument("--requester", help="Requester email — that row gets a '(you)' annotation")
    p.add_argument(
        "--names",
        default="",
        help='Email→display-name overrides as "email=Name,email=Name". Missing '
             "entries fall back to the email's local-part. Use semicolons "
             "inside names if a name itself needs commas (escape via backslash "
             "in shells that interpret).",
    )
    p.add_argument("--rank", type=int, help="Rank number for the slot header prefix (single-slot mode)")
    p.add_argument("--from", dest="from_file", help="Read full freebusy output from a file; render top --top slots")
    p.add_argument("--top", type=int, default=5, help="When using --from, how many slots to render")
    p.add_argument("--tick-minutes", type=int, default=DEFAULT_TICK_MINUTES)
    p.add_argument("--max-attendees", type=int, default=DEFAULT_MAX_VISIBLE_ATTENDEES)
    args = p.parse_args()

    attendees = [e.strip() for e in args.attendees.split(",") if e.strip()]
    names = parse_names_arg(args.names)

    if args.from_file:
        with open(args.from_file) as f:
            data = json.load(f)
        slots = (data.get("ranked_slots") or [])[: args.top]
        cards = [
            format_slot_card(
                slot,
                attendees=attendees,
                requester_email=args.requester,
                rank=i + 1,
                tick_minutes=args.tick_minutes,
                max_visible_attendees=args.max_attendees,
                names=names,
            )
            for i, slot in enumerate(slots)
        ]
        print("\n\n".join(cards))
        return

    slot = json.load(sys.stdin)
    print(
        format_slot_card(
            slot,
            attendees=attendees,
            requester_email=args.requester,
            rank=args.rank,
            tick_minutes=args.tick_minutes,
            max_visible_attendees=args.max_attendees,
            names=names,
        )
    )


if __name__ == "__main__":
    main()
