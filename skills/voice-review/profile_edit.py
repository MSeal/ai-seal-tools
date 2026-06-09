#!/usr/bin/env python3
"""Post-merge edits to the live voice profile.

Once exemplars are merged into `profile.yaml`, the normal way to change
your mind is to write a follow-up proposal. But sometimes you just need
to surgically remove a few bad merges, restore one you rejected by
mistake, or fix the reviewer_notes on existing exemplars. This module
provides those operations.

Every edit appends a `manual_edit` entry to the profile's merge_history
so the audit trail stays intact.

CLI:
    uv run skills/voice-review/profile_edit.py remove EX_ID [EX_ID ...]
    uv run skills/voice-review/profile_edit.py restore EX_ID --reason R [--notes N] [--substitute]
    uv run skills/voice-review/profile_edit.py update-notes EX_ID --reason R [--notes N]

`restore` searches the archive for the exemplar by ID and re-adds it
to its original audience/doc_type bucket (honoring override_* fields
on the archived proposal). Pass `--substitute` to apply the same
auto-substitution the merge path uses (placeholder names + <N> for
multi-digit numbers).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "voice-review"))

from reviewer import (  # noqa: E402
    ARCHIVE_DIR,
    PROFILE_PATH,
    _now,
    load_profile,
    save_profile,
)
from substituter import substitute_exemplar  # noqa: E402


def build_reviewer_notes(reason: str, notes: str | None) -> str:
    """Compose the reviewer_notes string in the format apply_exemplar_review
    uses, so manually-edited and merge-pipeline notes are visually identical."""
    parts = ["accepted despite scrub flag", f"reason: {reason}"]
    if notes:
        parts.append(f"notes: {notes}")
    return "; ".join(parts)


def remove_exemplars(profile: dict, ids: set[str]) -> list[tuple[str, str, str]]:
    """Drop every exemplar with an id in `ids`. Returns list of
    (ex_id, audience, doc_type) tuples for what was actually removed."""
    removed: list[tuple[str, str, str]] = []
    for aud_name, aud in profile.get("audiences", {}).items():
        for dt_name, dt in aud.get("types", {}).items():
            kept = []
            for ex in dt.get("exemplars", []):
                if ex.get("id") in ids:
                    removed.append((ex["id"], aud_name, dt_name))
                else:
                    kept.append(ex)
            dt["exemplars"] = kept
    return removed


def find_in_archive(ex_id: str, archive_dir: Path = ARCHIVE_DIR) -> tuple[dict, str, str] | None:
    """Search archived proposals for an exemplar by id. Returns
    (exemplar_dict, audience, doc_type) — honoring override_audience /
    override_doc_type when set, else falling back to classification."""
    for p in sorted(archive_dir.glob("prop_*.yaml")):
        try:
            d = yaml.safe_load(p.read_text())
        except yaml.YAMLError:
            continue
        for ex in d.get("candidate_exemplars", []):
            if ex.get("id") == ex_id:
                audience = d.get("override_audience") or d["classification"]["audience"]
                doc_type = d.get("override_doc_type") or d["classification"]["doc_type"]
                return ex, audience, doc_type
    return None


def restore_exemplar(
    profile: dict,
    ex_id: str,
    reason: str,
    notes: str | None = None,
    substitute: bool = False,
    archive_dir: Path = ARCHIVE_DIR,
    now: str | None = None,
) -> tuple[str, str] | None:
    """Pull an archived exemplar back into the profile. Returns
    (audience, doc_type) on success, None if not found in archive
    or already present in the profile."""
    found = find_in_archive(ex_id, archive_dir=archive_dir)
    if found is None:
        return None
    ex_dict, audience, doc_type = found

    bucket = profile["audiences"][audience]["types"][doc_type]
    if any(e.get("id") == ex_id for e in bucket.get("exemplars", [])):
        return None

    if substitute:
        ex_dict, _ = substitute_exemplar(ex_dict, substitute_numbers=True)

    new_ex = {
        "id": ex_dict["id"],
        "source_hash": ex_dict["source_hash"],
        "pattern": ex_dict["pattern"],
        "synthetic": ex_dict["synthetic"],
        "when_to_use": ex_dict["when_to_use"],
        "reviewed_at": now or _now(),
        "reviewer_notes": build_reviewer_notes(reason, notes),
    }
    bucket.setdefault("exemplars", []).append(new_ex)
    return audience, doc_type


def update_reviewer_notes(
    profile: dict,
    ex_id: str,
    reason: str,
    notes: str | None = None,
    now: str | None = None,
) -> tuple[str, str] | None:
    """Replace the reviewer_notes (and reviewed_at) of an existing
    exemplar. Returns (audience, doc_type) on success, None if the
    exemplar isn't in the profile."""
    for aud_name, aud in profile.get("audiences", {}).items():
        for dt_name, dt in aud.get("types", {}).items():
            for ex in dt.get("exemplars", []):
                if ex.get("id") == ex_id:
                    ex["reviewer_notes"] = build_reviewer_notes(reason, notes)
                    ex["reviewed_at"] = now or _now()
                    return aud_name, dt_name
    return None


def _append_history(profile: dict, action: str, **payload) -> None:
    profile.setdefault("merge_history", []).append({
        "merged_at": _now(),
        "action": action,
        **payload,
    })


def cmd_remove(args: argparse.Namespace) -> int:
    profile = load_profile()
    removed = remove_exemplars(profile, set(args.ids))
    if not removed:
        print("No matching exemplars in profile.")
        return 0
    for ex_id, aud, dt in removed:
        print(f"  removed {ex_id} from {aud}/{dt}")
    profile["last_updated"] = _now()
    _append_history(profile, "manual_edit_remove",
                    removed_exemplars=[r[0] for r in removed],
                    notes=args.message or "manual removal via profile_edit")
    save_profile(profile)
    print(f"Removed {len(removed)} exemplars.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    profile = load_profile()
    result = restore_exemplar(
        profile, args.id, reason=args.reason, notes=args.notes, substitute=args.substitute,
    )
    if result is None:
        print(f"Not found in archive or already present: {args.id}", file=sys.stderr)
        return 1
    audience, doc_type = result
    profile["last_updated"] = _now()
    _append_history(profile, "manual_edit_restore",
                    added_exemplars=[args.id],
                    substitute=args.substitute,
                    notes=args.message or f"restored from archive with reason={args.reason}")
    save_profile(profile)
    print(f"Restored {args.id} to {audience}/{doc_type}.")
    return 0


def cmd_update_notes(args: argparse.Namespace) -> int:
    profile = load_profile()
    result = update_reviewer_notes(profile, args.id, reason=args.reason, notes=args.notes)
    if result is None:
        print(f"Exemplar not in profile: {args.id}", file=sys.stderr)
        return 1
    audience, doc_type = result
    profile["last_updated"] = _now()
    _append_history(profile, "manual_edit_update_notes",
                    updated_notes_for=[args.id],
                    notes=args.message or f"reviewer_notes updated to reason={args.reason}")
    save_profile(profile)
    print(f"Updated reviewer_notes for {args.id} in {audience}/{doc_type}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_remove = sub.add_parser("remove", help="Delete exemplars from the profile by id")
    p_remove.add_argument("ids", nargs="+")
    p_remove.add_argument("--message", "-m", help="Optional note appended to merge_history")
    p_remove.set_defaults(func=cmd_remove)

    p_restore = sub.add_parser("restore", help="Re-add an archived exemplar to the profile")
    p_restore.add_argument("id")
    p_restore.add_argument("--reason", required=True,
                           help="Reason category (e.g. true_positive:override, false_positive:common_word)")
    p_restore.add_argument("--notes", help="Free-text notes")
    p_restore.add_argument("--substitute", action="store_true",
                           help="Apply auto-substitution (placeholder names, <N> for numbers)")
    p_restore.add_argument("--message", "-m")
    p_restore.set_defaults(func=cmd_restore)

    p_notes = sub.add_parser("update-notes", help="Replace reviewer_notes on an existing exemplar")
    p_notes.add_argument("id")
    p_notes.add_argument("--reason", required=True)
    p_notes.add_argument("--notes")
    p_notes.add_argument("--message", "-m")
    p_notes.set_defaults(func=cmd_update_notes)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
