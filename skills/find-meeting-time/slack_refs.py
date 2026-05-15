"""Slack reference resolver — turn `@handle` and `#channel` into the
Calendar identities (email addresses) the helper needs.

Source-agnostic by design. Currently fed by Glean (see slack_refs_glean.py
for the people-record + channel-search parsers). When the Slack MCP gets
approved, add slack_refs_slack.py with the same parse contract and swap
the data source without touching the cache layout, CLI, or downstream
consumers.

Storage layout (`~/.config/ai-seal-tools/find-meeting-time/slack_refs.yaml`,
Drive-backed via links.yaml):

    handles:
      eve:                       # rich record (from a Glean fetch)
        email: eve@example.com
        source: glean
        fetched_at: 2026-05-15T...
      alice: alice@example.com      # bare-string shorthand (hand-curated)

    channels:
      dtx-eng:
        members:
          - alice@example.com
          - bob@example.com
        source: glean (best-effort)
        note: "Members inferred from recent message authors; may be incomplete."
        fetched_at: 2026-05-15T...

Channel resolution is **best-effort** via Glean today — we can pull recent
message authors from a channel, but that's not the same as authoritative
channel membership. Anything we record carries a `note` field so Claude
(and the user reading the YAML) knows the limitation.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SlackHandle:
    """Resolved one-to-one mapping handle ↔ email. Handle is stored
    without the leading '@'."""
    handle: str
    email: str
    source: str = "unknown"


@dataclass(frozen=True)
class SlackChannel:
    """Resolved channel → list of member emails. Name is stored without
    the leading '#'. `note` documents any caveat (e.g., best-effort
    inference from recent authors)."""
    name: str
    member_emails: tuple[str, ...]
    source: str = "unknown"
    note: str = ""


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_handles(path: Path) -> dict[str, dict]:
    """Read the `handles` section of slack_refs.yaml. Returns
    handle (lowercased, no leading '@') → record dict carrying at least
    'email'. Missing file or section → empty dict."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("handles") or {}
    out: dict[str, dict] = {}
    for handle, entry in raw.items():
        h = str(handle).lstrip("@").lower()
        if isinstance(entry, str):
            out[h] = {"email": entry.lower()}
        elif isinstance(entry, dict) and "email" in entry:
            out[h] = {**entry, "email": str(entry["email"]).lower()}
        else:
            print(f"[slack_refs] skipping malformed handle entry {handle!r}: {entry!r}", file=sys.stderr)
    return out


def load_channels(path: Path) -> dict[str, dict]:
    """Read the `channels` section of slack_refs.yaml. Returns
    channel name (lowercased, no leading '#') → record dict carrying
    'members' (list of lowercased emails). Missing file or section →
    empty dict."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("channels") or {}
    out: dict[str, dict] = {}
    for name, entry in raw.items():
        n = str(name).lstrip("#").lower()
        if not isinstance(entry, dict) or "members" not in entry:
            print(f"[slack_refs] skipping malformed channel entry {name!r}: {entry!r}", file=sys.stderr)
            continue
        members = entry.get("members") or []
        if not isinstance(members, list):
            print(f"[slack_refs] channel {name!r} 'members' must be a list", file=sys.stderr)
            continue
        out[n] = {**entry, "members": [str(m).lower() for m in members if m]}
    return out


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def lookup_handle(query: str, handles: dict[str, dict]) -> str | None:
    """Resolve a Slack handle reference to an email. Accepts '@alice'
    or 'alice', case-insensitive. Returns None if not cached."""
    h = query.lstrip("@").lower()
    return handles.get(h, {}).get("email")


def lookup_channel(name: str, channels: dict[str, dict]) -> list[str]:
    """Resolve a Slack channel reference to its member emails. Accepts
    '#dtx-eng' or 'dtx-eng', case-insensitive. Returns [] if not cached."""
    n = name.lstrip("#").lower()
    return list(channels.get(n, {}).get("members", []))


def reverse_email_to_handle(email: str, handles: dict[str, dict]) -> str | None:
    """Reverse lookup: given an email, find the cached handle. Used for
    rendering `@mention` in Slack-formatted ask-messages."""
    e = email.lower()
    for handle, record in handles.items():
        if record.get("email") == e:
            return handle
    return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_handle(path: Path, h: SlackHandle) -> None:
    """Idempotent write of one handle entry. Replaces any existing entry
    for the same handle. Preserves channels section and other entries."""
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    handles = data.setdefault("handles", {})
    handles[h.handle.lstrip("@").lower()] = {
        "email": h.email.lower(),
        "source": h.source,
        "fetched_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))


def write_channel(path: Path, c: SlackChannel) -> None:
    """Idempotent write of one channel entry. Replaces any existing
    entry for the same channel name. Preserves handles section."""
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    channels = data.setdefault("channels", {})
    record: dict = {
        "members": sorted({m.lower() for m in c.member_emails if m}),
        "source": c.source,
        "fetched_at": _now_iso(),
    }
    if c.note:
        record["note"] = c.note
    channels[c.name.lstrip("#").lower()] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
