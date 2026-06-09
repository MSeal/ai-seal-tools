#!/usr/bin/env python3
"""Interactive walk-through of pending proposals → profile merge.

Sub-commands:
  list                   Show pending proposals (id, audience, doc_type, exemplar count)
  walk                   Walk through all pending proposals interactively
    --proposal <path>    Walk only this specific proposal
    --yes                Auto-accept stats and descriptors (NOT exemplars — those
                         still require per-item review)
    --dry-run            Show what would change without writing

Three pieces of every proposal:
  1. stats — merged via weighted average into the audience.types.<doc_type> bucket
  2. descriptors — current values are surfaced; LLM consolidates with new candidates
     IF user accepts the descriptor block
  3. exemplars — STRICT per-item review. Flagged exemplars default to reject.
     Accepted exemplars get a fresh source_hash link and reviewed_at timestamp.

The reviewer never auto-accepts exemplars. Stats and descriptors can be
auto-accepted with --yes but exemplars always prompt.

For testing, `apply_proposal_to_profile()` is a pure function that takes a
proposal dict + reviewer decisions and returns an updated profile dict.
Filesystem operations sit at the CLI layer.
"""
from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make skills/voice-analyze importable since we share schema + lexicons
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "voice-analyze"))

import yaml  # noqa: E402

import schema as voice_schema  # noqa: E402
from lexicons import VALID_AUDIENCES, VALID_DOC_TYPES  # noqa: E402
from scrub import scrub  # noqa: E402

VOICE_DIR = Path.home() / ".config" / "ai-seal-tools" / "voice"
PROFILE_PATH = VOICE_DIR / "profile.yaml"
SOURCES_SEEN_PATH = VOICE_DIR / "sources_seen.yaml"
PROPOSALS_DIR = VOICE_DIR / "proposals"
ARCHIVE_DIR = VOICE_DIR / "archive"

SOURCES_COUNT_CAP = 30


# ---------- Load / save -----------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    return yaml.safe_load(path.read_text()) or {}


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    data = load_yaml(path)
    voice_schema.validate("profile", data)
    return data


def save_profile(data: dict[str, Any], path: Path = PROFILE_PATH) -> None:
    data["last_updated"] = _now()
    voice_schema.validate("profile", data)
    save_yaml(path, data)


def load_sources_seen(path: Path = SOURCES_SEEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "sources": []}
    data = load_yaml(path)
    voice_schema.validate("sources_seen", data)
    return data


def save_sources_seen(data: dict[str, Any], path: Path = SOURCES_SEEN_PATH) -> None:
    voice_schema.validate("sources_seen", data)
    save_yaml(path, data)


def list_pending(proposals_dir: Path = PROPOSALS_DIR) -> list[Path]:
    if not proposals_dir.is_dir():
        return []
    out = []
    for p in sorted(proposals_dir.glob("prop_*.yaml")):
        try:
            d = yaml.safe_load(p.read_text())
            if (d or {}).get("review_status") == "pending":
                out.append(p)
        except (yaml.YAMLError, OSError):
            continue
    return out


# ---------- Merge math ------------------------------------------------------

def weighted_merge_number(
    old_value: float | None,
    new_value: float,
    old_count: int,
    k: int = 1,
    cap: int = SOURCES_COUNT_CAP,
) -> float:
    """Weighted average. If old_value is None, just return new_value.

    Capping old_count at `cap` lets the profile keep evolving instead of
    ossifying after many merges.
    """
    if old_value is None:
        return new_value
    effective = min(old_count, cap)
    return (effective * old_value + k * new_value) / (effective + k)


def merge_stats_block(
    old_stats: dict[str, Any],
    new_stats: dict[str, Any],
    old_count: int,
    k: int = 1,
) -> dict[str, Any]:
    """Merge two stats blocks via weighted average on numeric leaves.

    Walks each block recursively, merging numbers by weighted average and
    treating new dict keys as additions (no old value).
    """
    if not old_stats:
        return deepcopy(new_stats)
    out = deepcopy(old_stats)
    for key, new_val in new_stats.items():
        if isinstance(new_val, (int, float)):
            old_val = old_stats.get(key) if isinstance(old_stats.get(key), (int, float)) else None
            out[key] = weighted_merge_number(old_val, new_val, old_count, k=k)
        elif isinstance(new_val, dict):
            out[key] = merge_stats_block(old_stats.get(key, {}) or {}, new_val, old_count, k=k)
        else:
            # Lists or other types: replace wholesale. The stats schema doesn't
            # currently have list-typed numeric fields that need this.
            out[key] = deepcopy(new_val)
    return out


# ---------- Audience bucket helpers -----------------------------------------

def ensure_audience_bucket(
    profile: dict[str, Any],
    audience: str,
    doc_type: str,
    axis_estimates: dict[str, float],
) -> dict[str, Any]:
    """Return the per-doc-type sub-bucket for (audience, doc_type), creating
    parent structures as needed. Initializes axis_baseline from the first
    contributing source's axis_estimates."""
    audiences = profile.setdefault("audiences", {})
    if audience not in audiences:
        registry = profile.get("audience_registry", {})
        description = registry.get(audience, audience)
        audiences[audience] = {
            "description": description,
            "sources_count": 0,
            "axis_baseline": dict(axis_estimates),
            "types": {},
        }
    aud_block = audiences[audience]
    aud_block.setdefault("types", {})
    if doc_type not in aud_block["types"]:
        aud_block["types"][doc_type] = {
            "sources_count": 0,
            "stats": {},
            "descriptors": {},
            "exemplars": [],
        }
    return aud_block["types"][doc_type]


def update_axis_baseline(
    profile: dict[str, Any],
    audience: str,
    new_axes: dict[str, float],
    old_count: int,
) -> None:
    """Weighted-average update of the audience's axis baseline."""
    audiences = profile.setdefault("audiences", {})
    if audience not in audiences:
        return
    baseline = audiences[audience].setdefault("axis_baseline", dict(new_axes))
    for key in ("formality", "technical_density", "brevity", "warmth"):
        if key in new_axes:
            baseline[key] = weighted_merge_number(
                baseline.get(key), float(new_axes[key]), old_count, k=1
            )


# ---------- Descriptor merge ------------------------------------------------

def merge_descriptors(
    old_descriptors: dict[str, Any],
    new_descriptors: dict[str, Any],
    accepted_fields: set[str],
) -> dict[str, Any]:
    """Naive merge: for accepted fields, replace prose fields wholesale and
    append+dedup list fields. The full LLM consolidation path is a planned
    enhancement — see SKILL.md.
    """
    out = deepcopy(old_descriptors) if old_descriptors else {}
    list_fields = {
        "rhetorical_moves", "tics", "structural_habits", "openings_inventory",
        "closings_inventory", "what_to_avoid",
    }
    prose_fields = {
        "voice_summary", "transition_style", "humor_register", "self_reference_behavior",
    }
    for field in accepted_fields:
        new_val = new_descriptors.get(field)
        if new_val is None:
            continue
        if field in list_fields and isinstance(new_val, list):
            existing = out.get(field) or []
            seen = {str(x).strip().lower() for x in existing}
            for item in new_val:
                if str(item).strip().lower() not in seen:
                    existing.append(item)
                    seen.add(str(item).strip().lower())
            out[field] = existing
        elif field in prose_fields and isinstance(new_val, str):
            # Replace wholesale on accept (latest wins for prose)
            out[field] = new_val
        else:
            out[field] = deepcopy(new_val)
    return out


# ---------- Exemplar processing ---------------------------------------------

def accept_exemplar(
    cand: dict[str, Any],
    source_hash: str,
    reviewer_notes: str = "",
) -> dict[str, Any]:
    """Build a profile-shaped exemplar from a proposal candidate."""
    out = {
        "id": cand["id"],
        "source_hash": source_hash,
        "pattern": cand["pattern"],
        "synthetic": cand["synthetic"],
        "when_to_use": cand["when_to_use"],
        "reviewed_at": _now(),
    }
    if reviewer_notes:
        out["reviewer_notes"] = reviewer_notes
    return out


# ---------- Top-level merge -------------------------------------------------

def apply_proposal_to_profile(
    profile: dict[str, Any],
    proposal: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """Pure function: take a profile + proposal + reviewer decisions, return
    the updated profile dict.

    decisions = {
        "merge_stats": bool,
        "merge_descriptors": bool,
        "accepted_descriptor_fields": set[str],
        "accepted_exemplar_ids": set[str],   # which exemplar IDs to merge in
        "exemplar_notes": dict[str, str],     # exemplar_id -> reviewer_notes
        "audience": str,
        "doc_type": str,
    }
    """
    profile = deepcopy(profile)
    audience = decisions["audience"]
    doc_type = decisions["doc_type"]
    axis_estimates = proposal["classification"]["axis_estimates"]

    bucket = ensure_audience_bucket(profile, audience, doc_type, axis_estimates)
    old_count = bucket.get("sources_count", 0)

    # Stats merge
    if decisions.get("merge_stats"):
        bucket["stats"] = merge_stats_block(
            bucket.get("stats") or {},
            proposal.get("proposed_stats") or {},
            old_count=old_count,
            k=1,
        )

    # Descriptor merge
    if decisions.get("merge_descriptors"):
        accepted_fields = decisions.get("accepted_descriptor_fields") or set()
        if accepted_fields:
            bucket["descriptors"] = merge_descriptors(
                bucket.get("descriptors") or {},
                proposal.get("proposed_descriptors") or {},
                accepted_fields=set(accepted_fields),
            )

    # Exemplars: append accepted ones
    accepted_ex_ids = set(decisions.get("accepted_exemplar_ids") or [])
    ex_notes = decisions.get("exemplar_notes") or {}
    source_hash = proposal["source_hash"]
    if accepted_ex_ids:
        existing_exs = bucket.setdefault("exemplars", [])
        existing_ids = {e.get("id") for e in existing_exs}
        for cand in proposal.get("candidate_exemplars", []):
            if cand["id"] in accepted_ex_ids and cand["id"] not in existing_ids:
                existing_exs.append(accept_exemplar(cand, source_hash, ex_notes.get(cand["id"], "")))

    # Bump sources_count and update axis_baseline
    bucket["sources_count"] = old_count + 1
    audiences = profile.setdefault("audiences", {})
    aud_block = audiences[audience]
    aud_block["sources_count"] = aud_block.get("sources_count", 0) + 1
    update_axis_baseline(profile, audience, axis_estimates, old_count=aud_block["sources_count"] - 1)

    # Append a merge_history entry
    history = profile.setdefault("merge_history", [])
    history.append({
        "timestamp": _now(),
        "docs_analyzed": 1,
        "schema_version_at_time": profile.get("schema_version", 1),
        "notes": f"merged proposal {proposal['proposal_id']} ({audience}/{doc_type})",
    })

    return profile


def update_sources_seen(
    sources_seen: dict[str, Any],
    proposal: dict[str, Any],
    audience: str,
    doc_type: str,
    contributed_exemplar_ids: list[str],
) -> dict[str, Any]:
    """Append a sources_seen record for this proposal."""
    sources_seen = deepcopy(sources_seen)
    sources = sources_seen.setdefault("sources", [])
    # Dedup by hash — if already there, don't re-add
    for s in sources:
        if s.get("hash") == proposal["source_hash"]:
            return sources_seen
    record = {
        "hash": proposal["source_hash"],
        "analyzed_at": _now(),
        "schema_version_at_time": 1,
        "audience": audience,
        "doc_type": doc_type,
        "word_count": proposal.get("source_word_count", 0),
        "axis_estimates": proposal["classification"]["axis_estimates"],
    }
    # Carry provenance fields from the proposal into the long-term source
    # record so later consumers can filter exemplars by medium / trace back.
    if proposal.get("source_type"):
        record["source_type"] = proposal["source_type"]
    if proposal.get("source_ref"):
        record["source_ref"] = proposal["source_ref"]
    # Authorship: only stamp 'partial' (full is the implicit default).
    if proposal.get("authorship") == "partial":
        record["authorship"] = "partial"
    if contributed_exemplar_ids:
        record["contributed_exemplar_ids"] = list(contributed_exemplar_ids)
    sources.append(record)
    return sources_seen


# ---------- Interactive prompts ---------------------------------------------

def _prompt(msg: str, valid: list[str], default: str | None = None) -> str:
    suffix = f" [{'/'.join(valid)}]"
    if default:
        suffix += f" (default: {default})"
    suffix += ": "
    while True:
        ans = input(msg + suffix).strip().lower()
        if not ans and default:
            return default
        if ans in valid:
            return ans
        print(f"  please enter one of: {', '.join(valid)}")


def review_proposal_interactive(
    proposal: dict[str, Any],
    profile: dict[str, Any],
    yes: bool = False,
) -> dict[str, Any]:
    """Walk a single proposal interactively. Returns a `decisions` dict."""
    cls = proposal["classification"]
    audience = proposal.get("override_audience") or cls["audience"]
    doc_type = proposal.get("override_doc_type") or cls["doc_type"]

    print()
    print("=" * 70)
    print(f"Proposal: {proposal['proposal_id']}")
    print(f"  audience: {audience}  (classifier said {cls['audience']} @ {cls['audience_confidence']:.2f})")
    print(f"  doc_type: {doc_type}  (classifier said {cls['doc_type']} @ {cls['doc_type_confidence']:.2f})")
    print(f"  source_hash: {proposal['source_hash'][:16]}...")
    print(f"  word count: {proposal.get('source_word_count', '?')}")
    print(f"  exemplars: {len(proposal.get('candidate_exemplars', []))}")
    n_flagged = sum(1 for e in proposal.get("candidate_exemplars", []) if e.get("scrub_status") == "flagged")
    if n_flagged:
        print(f"  flagged exemplars: {n_flagged} (will default to reject)")
    print()

    if audience not in VALID_AUDIENCES:
        print(f"  ! invalid audience {audience!r} — skipping this proposal")
        return {"skip": True}
    if doc_type not in VALID_DOC_TYPES:
        print(f"  ! invalid doc_type {doc_type!r} — skipping this proposal")
        return {"skip": True}

    # 1. Classification confirmation
    if not yes:
        ans = _prompt("Confirm audience+doc_type", ["yes", "skip"], default="yes")
        if ans == "skip":
            return {"skip": True}

    decisions: dict[str, Any] = {
        "audience": audience,
        "doc_type": doc_type,
        "skip": False,
    }

    # 2. Stats
    stats = proposal.get("proposed_stats") or {}
    print(f"  Stats block: {len(stats)} top-level keys ({', '.join(sorted(stats.keys())[:6])}...)")
    if yes:
        decisions["merge_stats"] = True
    else:
        decisions["merge_stats"] = _prompt("Merge stats?", ["yes", "no"], default="yes") == "yes"

    # 3. Descriptors
    descriptors = proposal.get("proposed_descriptors") or {}
    print()
    print(f"  Descriptors block has {len(descriptors)} fields:")
    accepted_descriptor_fields: set[str] = set()
    if yes:
        accepted_descriptor_fields = set(descriptors.keys())
        decisions["merge_descriptors"] = True
    else:
        merge_desc = _prompt("Merge descriptors?", ["yes", "no", "per-field"], default="yes")
        if merge_desc == "yes":
            decisions["merge_descriptors"] = True
            accepted_descriptor_fields = set(descriptors.keys())
        elif merge_desc == "per-field":
            decisions["merge_descriptors"] = True
            for field, val in descriptors.items():
                preview = str(val)[:120].replace("\n", " ")
                print(f"\n  -- {field}: {preview}{'...' if len(str(val)) > 120 else ''}")
                if _prompt(f"  accept {field}?", ["yes", "no"], default="yes") == "yes":
                    accepted_descriptor_fields.add(field)
        else:
            decisions["merge_descriptors"] = False
    decisions["accepted_descriptor_fields"] = accepted_descriptor_fields

    # 4. Exemplars (always interactive — never auto-accept)
    accepted_exemplar_ids: set[str] = set()
    exemplar_notes: dict[str, str] = {}
    print()
    print(f"  Exemplars ({len(proposal.get('candidate_exemplars', []))}):")
    for i, ex in enumerate(proposal.get("candidate_exemplars", []), start=1):
        flagged = ex.get("scrub_status") == "flagged"
        print()
        print(f"  [{i}/{len(proposal['candidate_exemplars'])}] {ex['id']}  {'(FLAGGED)' if flagged else ''}")
        print(f"    pattern:    {ex['pattern']}")
        print(f"    synthetic:  {ex['synthetic']}")
        print(f"    when:       {ex['when_to_use']}")
        if flagged:
            for f in ex.get("scrub_findings", []):
                print(f"    flag:       [{f['rule']}] {f['snippet']!r} — {f['detail']}")
        default_action = "reject" if flagged else "accept"
        choices = ["accept", "reject", "skip"]
        ans = _prompt("  decision", choices, default=default_action)
        if ans == "accept":
            accepted_exemplar_ids.add(ex["id"])
            if flagged:
                exemplar_notes[ex["id"]] = "accepted despite scrub flag (manual override)"
    decisions["accepted_exemplar_ids"] = accepted_exemplar_ids
    decisions["exemplar_notes"] = exemplar_notes

    return decisions


# ---------- CLI entry -------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    pending = list_pending()
    print(f"Pending proposals: {len(pending)}")
    for p in pending:
        try:
            d = yaml.safe_load(p.read_text())
            cls = d.get("classification", {})
            audience = d.get("override_audience") or cls.get("audience", "?")
            doc_type = d.get("override_doc_type") or cls.get("doc_type", "?")
            n_ex = len(d.get("candidate_exemplars", []))
            n_flag = sum(1 for e in d.get("candidate_exemplars", []) if e.get("scrub_status") == "flagged")
            print(f"  {p.name}  {audience}/{doc_type}  {n_ex} exemplars ({n_flag} flagged)")
        except Exception as e:
            print(f"  {p.name}  ERROR: {e}")
    return 0


def cmd_walk(args: argparse.Namespace) -> int:
    if args.proposal:
        proposals = [Path(args.proposal).expanduser()]
    else:
        proposals = list_pending()
    if not proposals:
        print("No pending proposals.")
        return 0
    print(f"Walking {len(proposals)} proposal(s)...")

    profile = load_profile()
    sources_seen = load_sources_seen()
    n_merged = 0
    n_skipped = 0

    for path in proposals:
        proposal = yaml.safe_load(path.read_text())
        decisions = review_proposal_interactive(proposal, profile, yes=args.yes)
        if decisions.get("skip"):
            n_skipped += 1
            print(f"  → skipped {path.name}")
            continue
        profile = apply_proposal_to_profile(profile, proposal, decisions)
        sources_seen = update_sources_seen(
            sources_seen,
            proposal,
            audience=decisions["audience"],
            doc_type=decisions["doc_type"],
            contributed_exemplar_ids=list(decisions.get("accepted_exemplar_ids") or []),
        )
        proposal["review_status"] = "accepted"
        if args.dry_run:
            print(f"  (dry run) would merge {path.name}")
        else:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            archive_path = ARCHIVE_DIR / path.name
            archive_path.write_text(yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True))
            path.unlink()
            print(f"  → merged {path.name} (archived)")
        n_merged += 1

    if args.dry_run:
        print(f"\n(dry run — no files changed) {n_merged} would-merge, {n_skipped} skipped")
        return 0

    save_profile(profile)
    save_sources_seen(sources_seen)
    print(f"\nDone. {n_merged} merged, {n_skipped} skipped. Profile + sources_seen updated.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Show pending proposals")
    p_list.set_defaults(func=cmd_list)

    p_walk = sub.add_parser("walk", help="Walk pending proposals interactively")
    p_walk.add_argument("--proposal", help="Walk only this specific proposal file")
    p_walk.add_argument("--yes", action="store_true", help="Auto-accept stats + descriptors (NOT exemplars)")
    p_walk.add_argument("--dry-run", action="store_true", help="Don't write files; show what would change")
    p_walk.set_defaults(func=cmd_walk)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
