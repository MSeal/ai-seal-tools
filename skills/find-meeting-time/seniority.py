"""Seniority contract: resolve an attendee email to a tier (0-5) so the
scoring engine can penalize slots whose conflicting meetings include
senior org members. Sourcing is pluggable: see seniority_glean.py for
the Glean-flavored implementation; other directories (LDAP, SCIM, manual
entry) only need to produce the same SeniorityFields shape.

Storage (always YAML on disk for auditability):
    ~/.config/ai-seal-tools/find-meeting-time/seniority.yaml
        seniority:
          alice@example.com:
            tier: 4
            title: "VP, Engineering"
            department: "Engineering"
            source: "glean"
            fetched_at: "2026-05-15T..."
          # Or a bare integer for hand-curated entries:
          bob@example.com: 3

Lookups are case-insensitive on email. Unlisted attendees default to
tier 0 (no penalty). Tier scale, by convention:
    0 = Default / IC
    1 = Senior IC ("Senior", "Staff" prefix) or first-level Manager
    2 = Principal IC or Senior Manager
    3 = Director (or Director II)
    4 = VP / Vice President
    5 = SVP / EVP / C-level
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SeniorityFields:
    """Raw fields from a directory lookup. Whatever the source, the rest
    of the system only sees this shape. Add fields here when a new source
    surfaces something the inference function should consider — never
    couple inference to source-specific JSON shapes."""
    email: str
    title: str | None = None
    department: str | None = None
    teams: tuple[str, ...] = ()
    manager_title: str | None = None
    total_reports_count: int | None = None
    source: str = "unknown"


# Title-prefix rules ordered by specificity. First match wins.
# Tested separately in tests/test_seniority.py; add cases there when
# you find a title that mis-classifies in your org.
_TITLE_RULES: list[tuple[re.Pattern[str], int]] = [
    # C-level / chiefs
    (re.compile(r"\b(chief\b|cto\b|ceo\b|cfo\b|coo\b|cso\b|cmo\b|cpo\b|ciso\b)", re.I), 5),
    # SVP / EVP
    (re.compile(r"\b(svp|evp|senior\s*vice\s*president|executive\s*vice\s*president)\b", re.I), 5),
    # VP / Vice President (but not SVP/EVP, handled above)
    (re.compile(r"\b(vp|vice\s*president)\b", re.I), 4),
    # Director (incl. Sr Director, Director II)
    (re.compile(r"\b(director)\b", re.I), 3),
    # Principal-level IC or Senior Manager
    (re.compile(r"\b(principal|distinguished|senior\s*manager|sr\.?\s*manager|architect)\b", re.I), 2),
    # First-level manager (alone, no "senior") or Staff/Senior IC
    (re.compile(r"\b(staff|senior|lead|sr\.?)\b", re.I), 1),
    (re.compile(r"\b(manager|head\s*of)\b", re.I), 1),
]


def infer_tier(fields: SeniorityFields) -> int:
    """Map raw directory fields → 0-5 tier using title patterns.

    Pure function. The same SeniorityFields produces the same tier
    regardless of source. Add new title patterns to _TITLE_RULES if
    your directory uses titles this doesn't cover.
    """
    title = (fields.title or "").strip()
    if not title:
        return 0
    for pattern, tier in _TITLE_RULES:
        if pattern.search(title):
            return tier
    return 0


def load_seniority(path: Path) -> dict[str, dict]:
    """Read seniority.yaml. Returns email (lowercased) → record dict.
    Each record has at least 'tier'; may also carry title, department,
    source, fetched_at, etc. Missing file → empty dict (no penalty
    anywhere). Bare-integer entries are normalized to {'tier': N}.
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("seniority") or {}
    out: dict[str, dict] = {}
    for email, entry in raw.items():
        e = str(email).lower()
        if isinstance(entry, int):
            out[e] = {"tier": entry}
        elif isinstance(entry, dict) and "tier" in entry:
            out[e] = {**entry, "tier": int(entry["tier"])}
        else:
            print(f"[seniority] skipping malformed entry for {email!r}: {entry!r}", file=sys.stderr)
    return out


def tier_for(email: str, seniority_map: dict[str, dict]) -> int:
    """Lookup helper. Unknown emails → 0 (no penalty)."""
    return seniority_map.get(email.lower(), {}).get("tier", 0)


def write_record(path: Path, fields: SeniorityFields, tier: int | None = None) -> None:
    """Idempotent write to seniority.yaml. Replaces any existing entry
    for the email. If tier is None, runs infer_tier(fields)."""
    if tier is None:
        tier = infer_tier(fields)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    sen = data.setdefault("seniority", {})
    record: dict = {"tier": tier}
    if fields.title:
        record["title"] = fields.title
    if fields.department:
        record["department"] = fields.department
    if fields.manager_title:
        record["manager_title"] = fields.manager_title
    if fields.total_reports_count is not None:
        record["total_reports_count"] = fields.total_reports_count
    record["source"] = fields.source
    record["fetched_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sen[fields.email.lower()] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
