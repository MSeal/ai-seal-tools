"""Source index management: heuristic audience/doc_type categorization,
dedup, rollup summaries, and read/write of source_index.yaml.

The audience heuristic is intentionally simple — title keywords + Confluence
space → tag with a confidence score. It's a first pass, not the source of
truth. The LLM classifier is more accurate but expensive; the heuristic
gives us fast triage for big corpora. Each entry records `audience_source`
(heuristic | classifier | manual) so we know what to trust.

Adding a new source location:
    1. Add it to the SOURCE_LOCATIONS enum in schemas/source_index_v1.json
    2. Update LOCATION_HEURISTICS below if there's a useful default
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

import schema as voice_schema


INDEX_PATH = Path.home() / ".config" / "ai-seal-tools" / "voice" / "source_index.yaml"


# ---------- Categorization heuristic ----------------------------------------

# Each rule is (regex, audience_tag, confidence, doc_type_hint_or_None).
# Rules are checked in order; first match wins. Regex is matched against
# title.lower(). Both the audience and the optional doc_type hint are
# returned so a single rule can carry both signals when they're correlated.
_TITLE_RULES: list[tuple[re.Pattern, str, float, str | None]] = [
    # External-facing / public
    (re.compile(r"\b(blog post|conference talk|talk draft|talk outline|conference|meetup|public readme|external announcement)\b"), "external_public", 0.7, None),
    (re.compile(r"\b(bangalore|austin|current 20|conference) (talk|draft|outline|abstract)"), "external_public", 0.8, "outline"),

    # Leadership / strategy / status
    (re.compile(r"\b(leadership sync|leadership meeting|exec sync|go.no.go|go/no.go|nrr|business goals?|business case|roi case|okrs?|q[1-4]\s*planning|annual plan|quarterly plan|roadmap|status report|operational suggestions)\b"), "leadership", 0.7, None),
    (re.compile(r"\b(team structure|hiring|headcount|reorg|reorganization|principal eng review)\b"), "leadership", 0.75, None),

    # Cross-functional (PM/design partnership)
    (re.compile(r"\b(target persona|partnership asks?|cross[- ]functional|product strategy|design review|requirements|spec|specification|primer|cuj)\b"), "cross_functional", 0.65, None),

    # Technical peer (the largest bucket for an IC engineer)
    (re.compile(r"\b(design note|design doc|design proposal|architecture|rfc|security review|test plan|test scenarios?|technical state|technical analysis|tech debt|tech state|review|spike|investigation|proof of concept|poc|migration plan|api reliability|future proofing|reliability)\b"), "technical_peer", 0.7, None),
    (re.compile(r"\b(scratchpad|wip|\[wip\]|draft)\b"), "technical_peer", 0.4, "draft"),

    # Direct-report / coaching (rare in confluence, more in 1:1 docs)
    (re.compile(r"\b(1:1|one on one|career growth|feedback session|coaching notes|performance review)\b"), "direct_report", 0.85, None),

    # Self-notes / personal
    (re.compile(r"\b(personal notes|todo list|brain dump|journal entry|scratch notes)\b"), "self_notes", 0.85, None),
]

# Confluence space → audience-bucket hint with low-to-medium confidence.
# These boost a title-based guess but don't override a high-confidence title
# rule. Spaces named here come from observed seed-doc spaces; extend as needed.
_SPACE_HINTS: dict[str, tuple[str, float]] = {
    "DTX": ("technical_peer", 0.55),
    "CLIENTS": ("technical_peer", 0.55),
    "AEGI": ("technical_peer", 0.55),
    "NEI": ("technical_peer", 0.55),
    "RP2": ("technical_peer", 0.55),
    "DESIGN": ("cross_functional", 0.55),
    # Confluence "personal space" pages (~userid as the key) → harder to call;
    # often these are drafts or scratch. Default to technical_peer with low
    # confidence; flag as draft hint.
}

# Title-token hints for doc_type independent of audience.
_DOC_TYPE_RULES: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"\b(outline|draft outline|scratchpad|talk outline)\b"), "outline", 0.85),
    (re.compile(r"\b(\[wip\]|\bwip\b|draft|first draft)\b"), "draft", 0.8),
]


def _is_personal_space(space: str | None) -> bool:
    """Confluence "personal space" page keys start with ~userid."""
    return bool(space) and space.startswith("~")


@dataclass
class HeuristicResult:
    audience: str
    audience_confidence: float
    audience_alternates: list[str]
    doc_type: str
    doc_type_confidence: float
    matched_rule: str | None = None


def categorize_title(title: str, space: str | None = None) -> HeuristicResult:
    """Apply title + space heuristics to guess audience and doc_type.

    Always returns a HeuristicResult — when nothing matches, falls back to
    technical_peer/polished at low confidence (the most common default for
    this user's writing pattern).
    """
    title_lower = (title or "").lower()
    matched: tuple[str, float, str | None, str] | None = None  # (audience, conf, doc_type_hint, rule_repr)

    for pattern, audience, conf, doc_type_hint in _TITLE_RULES:
        if pattern.search(title_lower):
            matched = (audience, conf, doc_type_hint, pattern.pattern[:60])
            break

    # Space hint as a fallback or supporting signal
    space_audience: str | None = None
    space_conf = 0.0
    if space and space in _SPACE_HINTS:
        space_audience, space_conf = _SPACE_HINTS[space]

    if matched is None:
        # No title match. Fall back to space hint if any, otherwise default.
        if space_audience:
            audience = space_audience
            confidence = space_conf
            rule = f"space:{space}"
        else:
            audience = "technical_peer"
            confidence = 0.3
            rule = "default"
        doc_type_hint = None
    else:
        audience, confidence, doc_type_hint, rule = matched
        # Boost confidence slightly if space hint corroborates the title rule
        if space_audience == audience:
            confidence = min(1.0, confidence + 0.1)

    # Independent doc_type pass — title can carry both an audience cue and a
    # doc_type cue independently.
    doc_type = "polished"
    doc_type_confidence = 0.5
    if doc_type_hint:
        doc_type = doc_type_hint
        doc_type_confidence = 0.7
    for pattern, dt, dt_conf in _DOC_TYPE_RULES:
        if pattern.search(title_lower) and dt_conf > doc_type_confidence:
            doc_type = dt
            doc_type_confidence = dt_conf
            break

    # Personal-space pages skew toward draft when title doesn't already hint
    if _is_personal_space(space) and doc_type == "polished" and doc_type_confidence < 0.6:
        doc_type = "draft"
        doc_type_confidence = 0.4

    # Build alternates: include space hint if it differs from title match
    alternates: list[str] = []
    if space_audience and space_audience != audience:
        alternates.append(space_audience)

    return HeuristicResult(
        audience=audience,
        audience_confidence=round(confidence, 2),
        audience_alternates=alternates,
        doc_type=doc_type,
        doc_type_confidence=round(doc_type_confidence, 2),
        matched_rule=rule,
    )


# ---------- Source-id generation --------------------------------------------

def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def generate_source_id(location: str, dedup_key: str) -> str:
    """Stable id from location + dedup key (url or path).

    Format: <location>_<12-char-hash>. Matches the source_entry.id regex.
    """
    return f"{location}_{_short_hash(dedup_key)}"


# ---------- Dedup key extraction --------------------------------------------

def dedup_key_for(entry: dict[str, Any]) -> str:
    """Return the key used to detect duplicates. URL for remote, path for local."""
    if entry.get("url"):
        return entry["url"].strip()
    if entry.get("path"):
        return str(Path(entry["path"]).expanduser().resolve())
    raise ValueError(f"entry has neither url nor path: {entry!r}")


# ---------- Index ops --------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    """Load + validate. If the file doesn't exist, return an empty v1 doc."""
    if not path.exists():
        return {"schema_version": 1, "sources": []}
    data = yaml.safe_load(path.read_text()) or {}
    voice_schema.validate("source_index", data)
    return data


def save_index(data: dict[str, Any], path: Path = INDEX_PATH) -> None:
    """Validate and write."""
    data.setdefault("schema_version", 1)
    data["last_updated"] = _now()
    voice_schema.validate("source_index", data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def add_entries(
    new_entries: Iterable[dict[str, Any]],
    index: dict[str, Any] | None = None,
    apply_heuristic: bool = True,
) -> dict[str, Any]:
    """Merge new entries into the index. Idempotent on dedup_key.

    Each new_entry must have: location, title, AND (url or path).
    Optional: space, updated_at, word_count, notes.

    If apply_heuristic is True, fills in proposed_audience/proposed_doc_type
    from the title+space heuristic when those fields are missing.

    Returns the merged index dict (does not write to disk — caller decides
    when to save).
    """
    if index is None:
        index = {"schema_version": 1, "sources": []}
    by_key: dict[str, dict[str, Any]] = {dedup_key_for(s): s for s in index["sources"]}
    added = 0
    updated = 0
    for entry in new_entries:
        entry = dict(entry)  # copy — we mutate
        key = dedup_key_for(entry)
        existing = by_key.get(key)

        # Apply heuristic if needed
        if apply_heuristic and "proposed_audience" not in entry:
            h = categorize_title(entry["title"], entry.get("space"))
            entry.setdefault("proposed_audience", h.audience)
            entry.setdefault("audience_confidence", h.audience_confidence)
            entry.setdefault("audience_source", "heuristic")
            if h.audience_alternates:
                entry.setdefault("audience_alternates", h.audience_alternates)
            entry.setdefault("proposed_doc_type", h.doc_type)
            entry.setdefault("doc_type_source", "heuristic")

        if existing:
            # Merge: prefer new values for updatable fields, but never downgrade
            # status or overwrite analyzed source_hash.
            preserved = {"id", "discovered_at", "status", "source_hash", "proposal_id"}
            for k, v in entry.items():
                if k in preserved:
                    continue
                existing[k] = v
            updated += 1
        else:
            entry.setdefault("id", generate_source_id(entry["location"], key))
            entry.setdefault("discovered_at", _now())
            entry.setdefault("status", "queued")
            entry.setdefault("contribution_type", "unknown")
            index["sources"].append(entry)
            by_key[key] = entry
            added += 1

    index.setdefault("generated_at", _now())
    index["last_updated"] = _now()
    index["_last_add_summary"] = {"added": added, "updated": updated}
    # _last_add_summary is non-schema metadata; strip before saving
    return index


def summary(index: dict[str, Any]) -> dict[str, Any]:
    """Build rollup counts: by audience, doc_type, location, status."""
    by_audience: dict[str, int] = {}
    by_doc_type: dict[str, int] = {}
    by_location: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_contribution: dict[str, int] = {}
    total = 0
    for s in index["sources"]:
        total += 1
        by_audience[s.get("proposed_audience", "unknown")] = by_audience.get(s.get("proposed_audience", "unknown"), 0) + 1
        by_doc_type[s.get("proposed_doc_type", "unknown")] = by_doc_type.get(s.get("proposed_doc_type", "unknown"), 0) + 1
        by_location[s["location"]] = by_location.get(s["location"], 0) + 1
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        ctype = s.get("contribution_type", "unknown")
        by_contribution[ctype] = by_contribution.get(ctype, 0) + 1
    # Cross: audience × doc_type
    cross: dict[str, dict[str, int]] = {}
    for s in index["sources"]:
        a = s.get("proposed_audience", "unknown")
        d = s.get("proposed_doc_type", "unknown")
        cross.setdefault(a, {})
        cross[a][d] = cross[a].get(d, 0) + 1
    return {
        "total": total,
        "by_audience": by_audience,
        "by_doc_type": by_doc_type,
        "by_location": by_location,
        "by_status": by_status,
        "by_contribution_type": by_contribution,
        "audience_x_doc_type": cross,
    }


def strip_internal_keys(index: dict[str, Any]) -> dict[str, Any]:
    """Remove non-schema metadata keys before saving (e.g. _last_add_summary)."""
    return {k: v for k, v in index.items() if not k.startswith("_")}


# ---------- Auto-skip pass for data-only artifacts --------------------------

# Title patterns that strongly suggest the doc is data/tracking rather than
# prose. We auto-mark these as `skipped` with a reason so they're filtered
# out of voice analysis without forcing the user to manually triage each.
_DATA_TITLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.(csv|tsv|xlsx?|json|txt|mp4|mov|png|jpe?g|pdf)$", re.IGNORECASE), "data-file extension in title"),
    (re.compile(r"\b(burndown|progress|stats|metric|metrics|breakdown|worktrack|backlog|tickets|tracker|tracking)\b", re.IGNORECASE), "tracking/data title keyword"),
    (re.compile(r"^Untitled\b", re.IGNORECASE), "untitled file"),
    (re.compile(r"\bsupporting data\b", re.IGNORECASE), "labeled supporting data"),
]

# Location-based skip: gdrive_sheets is virtually always data. Only "Note: …"
# or similar prose-bearing sheets would be exceptions; treat the location as
# strong-skip and require explicit override.
_DATA_LOCATIONS: frozenset[str] = frozenset({"gdrive_sheets", "gdrive_other"})


def auto_skip_data_entries(index: dict[str, Any]) -> dict[str, int]:
    """Mark clearly-data entries as status=skipped with skip_reason.

    Skips:
    - Entries in _DATA_LOCATIONS (gdrive_sheets, gdrive_other)
    - Entries whose title matches a _DATA_TITLE_PATTERNS rule

    Does NOT skip:
    - Entries already analyzed (status=analyzed) or already skipped
    - Entries in confluence, gdrive_doc, gdrive_slides, jira

    Returns counts: {skipped_now, already_skipped, kept}.
    """
    skipped_now = 0
    already_skipped = 0
    kept = 0
    for s in index["sources"]:
        if s.get("status") == "analyzed":
            kept += 1
            continue
        if s.get("status") == "skipped":
            already_skipped += 1
            continue

        reason: str | None = None
        if s["location"] in _DATA_LOCATIONS:
            reason = f"location={s['location']} (typically data, not prose)"
        else:
            title = s.get("title", "")
            for pattern, why in _DATA_TITLE_PATTERNS:
                if pattern.search(title):
                    reason = why
                    break

        if reason:
            s["status"] = "skipped"
            s["skip_reason"] = reason
            skipped_now += 1
        else:
            kept += 1
    return {"skipped_now": skipped_now, "already_skipped": already_skipped, "kept": kept}
