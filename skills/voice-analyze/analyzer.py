#!/usr/bin/env python3
"""CLI entry point for the voice-analyze skill.

Sub-commands:
  extract <doc-path>   Run the deterministic stats extractor and print JSON.
  classify <doc-path>  Run the LLM classifier and print JSON.
  propose <doc-path>   Full pipeline: classify + extract stats + descriptors +
                       exemplars, scrub-validate, write proposal YAML.

For testing, the orchestration helper `run_propose()` accepts injected
`llm`, `nlp`, and `lexicons` so tests can use mocked Claude without touching
the API. The CLI wrappers (`cmd_*`) handle real-world loading.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import spacy

from extractor import extract_stats, normalize_text
from fetcher import FetchUnavailable, fetch_content
from indexer import (
    INDEX_PATH,
    add_entries,
    auto_skip_data_entries,
    load_index,
    save_index,
    strip_internal_keys,
    summary,
)
from lexicons import VALID_AUDIENCES, VALID_DOC_TYPES, Lexicons, load_lexicons
from llm import CandidateExemplar, Classification, VoiceLLM, preflight_auth_check
from proposal import (
    PROPOSALS_DIR,
    ExemplarWithScrub,
    Proposal,
    new_exemplar_id,
    new_proposal_id,
    now_iso,
    write_proposal,
)
from scrub import ScrubFinding, scrub

SPACY_MODEL = "en_core_web_sm"


def load_nlp() -> spacy.Language:
    """Load the spaCy English model. Prints a helpful install hint on failure."""
    try:
        return spacy.load(SPACY_MODEL)
    except OSError:
        print(
            f"\nspaCy model '{SPACY_MODEL}' not installed.\n"
            f"Install it once with:\n\n"
            f"    UV_NO_CONFIG=1 uv run python -m spacy download {SPACY_MODEL}\n\n"
            f"(see skills/voice-analyze/SETUP.md for the full install steps).",
            file=sys.stderr,
        )
        sys.exit(2)


def compute_hash(text: str) -> str:
    """SHA-256 over normalized text. Used to dedup sources without storing content."""
    nfc = unicodedata.normalize("NFC", text).lower()
    nfc = " ".join(nfc.split())
    return hashlib.sha256(nfc.encode("utf-8")).hexdigest()


def read_doc(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"error: file not found: {path}")
    text = normalize_text(path.read_text())
    if not text:
        raise SystemExit(f"error: document is empty after normalization: {path}")
    return text


def word_count(nlp: spacy.Language, text: str) -> int:
    return len([t for t in nlp(text) if not t.is_space and not t.is_punct])


# ---------- extract ---------------------------------------------------------

def cmd_extract(args: argparse.Namespace) -> int:
    text = read_doc(Path(args.path))
    nlp = load_nlp()
    lexicons = load_lexicons()
    stats = extract_stats(text, nlp, lexicons)
    out: dict[str, Any] = {
        "source_hash": compute_hash(text),
        "word_count": word_count(nlp, text),
        "stats": stats,
        "lexicon_status": {
            "concreteness_loaded": lexicons.concreteness is not None,
            "dale_chall_loaded": lexicons.dale_chall_easy is not None,
        },
    }
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ---------- classify --------------------------------------------------------

def cmd_classify(args: argparse.Namespace) -> int:
    text = read_doc(Path(args.path))
    llm = VoiceLLM()
    classification = llm.classify(text)
    json.dump(classification.as_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ---------- propose ---------------------------------------------------------

@dataclass
class DescriptorLeak(Exception):
    """Raised when descriptor scrub finds a leak. Caller (cmd_propose) prints
    findings and exits non-zero — the proposal is never written."""
    findings: list[dict[str, Any]]

    def __str__(self) -> str:
        return f"descriptor scrub failed with {len(self.findings)} finding(s)"


def _scrub_descriptors_or_raise(descriptors: dict[str, Any], source_text: str) -> None:
    """Scrub every string field in the descriptors dict against source text.

    Raises DescriptorLeak with all collected findings if anything leaks.
    Descriptors are loud-failing because they go directly into the profile —
    we don't want flagged-but-included descriptors slipping through review.
    """
    findings: list[dict[str, Any]] = []
    for field_name, value in descriptors.items():
        if isinstance(value, str):
            result = scrub(value, source=source_text)
            for f in result.findings:
                findings.append(_finding_dict(f, where=f"descriptors:{field_name}"))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    continue
                result = scrub(item, source=source_text)
                for f in result.findings:
                    findings.append(_finding_dict(f, where=f"descriptors:{field_name}[{i}]"))
    if findings:
        raise DescriptorLeak(findings=findings)


def _finding_dict(f: ScrubFinding, where: str) -> dict[str, Any]:
    return {"rule": f.rule, "snippet": f.snippet, "detail": f.detail, "where": where}


def _scrub_exemplars(
    exemplars: list[CandidateExemplar],
    source_text: str,
    source_hash: str,
) -> list[ExemplarWithScrub]:
    """Scrub each exemplar's content fields against the source. Flagged
    exemplars stay in the proposal so the reviewer sees what to inspect —
    they do not halt the run.

    We scrub each field independently rather than concatenating, so that:
    1. The sentence-initial-capital allowance fires correctly for the first
       word of each field ('Use when...' in when_to_use is sentence-initial).
    2. Findings can attribute to the specific field that failed.
    """
    out: list[ExemplarWithScrub] = []
    for i, cand in enumerate(exemplars, start=1):
        ex_id = new_exemplar_id(source_hash, i)
        all_findings = []
        for field_name in ("synthetic", "pattern", "when_to_use"):
            value = getattr(cand, field_name)
            result = scrub(value, source=source_text)
            for f in result.findings:
                # Re-tag the finding with which field of this exemplar it came from
                from scrub import ScrubFinding
                all_findings.append(ScrubFinding(
                    rule=f.rule,
                    snippet=f.snippet,
                    detail=f"{f.detail} (in {field_name})",
                ))
        out.append(ExemplarWithScrub(
            id=ex_id,
            pattern_id=cand.pattern_id,
            pattern=cand.pattern,
            synthetic=cand.synthetic,
            when_to_use=cand.when_to_use,
            source_hash=source_hash,
            scrub_status="passed" if not all_findings else "flagged",
            scrub_findings=all_findings,
        ))
    return out


def _patterns_to_exemplify(descriptors: dict[str, Any], cap: int = 8) -> list[str]:
    """Choose which descriptor patterns to feed the exemplar generator.

    Prefers rhetorical_moves + opening/closing inventory. These are
    structural-and-reusable; tics and structural_habits are better suited to
    write-time prompting than to exemplification.
    """
    out: list[str] = []
    for source in ("rhetorical_moves", "openings_inventory", "closings_inventory"):
        for p in descriptors.get(source, []):
            if isinstance(p, str) and p.strip():
                out.append(p.strip())
                if len(out) >= cap:
                    return out
    return out


def run_propose(
    text: str,
    nlp: spacy.Language,
    lexicons: Lexicons,
    llm: VoiceLLM,
    override_audience: str | None = None,
    override_doc_type: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
) -> Proposal:
    """Build a Proposal from a document. Pure orchestration — no filesystem.

    Raises DescriptorLeak (caller decides how to surface) if the descriptor
    scrub fails. Exemplar leaks are flagged inline, not raised.
    """
    source_hash = compute_hash(text)
    classification = llm.classify(text)
    descriptors = llm.extract_descriptors(text)

    # Loud-fail on descriptor leakage — they go straight into the profile.
    _scrub_descriptors_or_raise(descriptors, source_text=text)

    patterns = _patterns_to_exemplify(descriptors)
    candidate_exemplars = llm.generate_exemplars(text, patterns)
    scrubbed_exemplars = _scrub_exemplars(candidate_exemplars, source_text=text, source_hash=source_hash)

    stats = extract_stats(text, nlp, lexicons)
    wc = word_count(nlp, text)

    proposal = Proposal(
        proposal_id=new_proposal_id(source_hash),
        created_at=now_iso(),
        source_hash=source_hash,
        source_word_count=wc,
        classification=classification,
        proposed_stats=stats,
        proposed_descriptors=descriptors,
        candidate_exemplars=scrubbed_exemplars,
        scrub_findings=[],  # descriptor-level leaks would have raised; exemplar findings live on each exemplar
        override_audience=override_audience,
        override_doc_type=override_doc_type,
        source_type=source_type,
        source_ref=source_ref,
    )
    return proposal


def cmd_propose(args: argparse.Namespace) -> int:
    if args.audience and args.audience not in VALID_AUDIENCES:
        print(f"error: --audience must be one of {sorted(VALID_AUDIENCES)}", file=sys.stderr)
        return 1
    if args.doc_type and args.doc_type not in VALID_DOC_TYPES:
        print(f"error: --doc-type must be one of {sorted(VALID_DOC_TYPES)}", file=sys.stderr)
        return 1

    text = read_doc(Path(args.path))
    nlp = load_nlp()
    lexicons = load_lexicons()
    llm = VoiceLLM()

    try:
        proposal = run_propose(
            text=text,
            nlp=nlp,
            lexicons=lexicons,
            llm=llm,
            override_audience=args.audience,
            override_doc_type=args.doc_type,
            source_type=args.source_type,
            source_ref=args.source_ref,
        )
    except DescriptorLeak as leak:
        print(
            f"\nDescriptor scrub failed — proposal NOT written. "
            f"{len(leak.findings)} leak(s) detected:\n",
            file=sys.stderr,
        )
        for f in leak.findings:
            print(f"  • [{f['rule']}] at {f['where']}: {f['snippet']!r} ({f['detail']})",
                  file=sys.stderr)
        print(
            "\nThis usually means the LLM didn't follow the anti-leak rules well "
            "enough on this document. Retry; if it persists, the descriptor "
            "prompt may need tightening.",
            file=sys.stderr,
        )
        return 3

    dest_dir = Path(args.proposals_dir).expanduser() if args.proposals_dir else PROPOSALS_DIR
    path = write_proposal(proposal, dest_dir)

    n_ex = len(proposal.candidate_exemplars)
    n_flagged = sum(1 for e in proposal.candidate_exemplars if e.scrub_status == "flagged")
    cls = proposal.classification
    audience = args.audience or cls.audience
    doc_type = args.doc_type or cls.doc_type
    print(
        f"Proposal written: {path}\n"
        f"  audience: {audience} (classifier said {cls.audience} @ {cls.audience_confidence:.2f})\n"
        f"  doc_type: {doc_type} (classifier said {cls.doc_type} @ {cls.doc_type_confidence:.2f})\n"
        f"  exemplar candidates: {n_ex} ({n_flagged} flagged by scrub)\n"
        f"  source word count: {proposal.source_word_count}\n"
        f"\nNext: /voice-review to walk through and accept/reject each piece."
    )
    return 0


# ---------- arg parsing -----------------------------------------------------

# ---------- index sub-commands ----------------------------------------------

def cmd_index_add(args: argparse.Namespace) -> int:
    """Add entries from a JSON file containing a list of source descriptors.

    JSON format: a list of objects with at least {location, title, url|path}.
    Optional fields: space, updated_at, word_count, proposed_audience, etc.
    """
    if not args.input_json:
        print("error: --input-json is required", file=sys.stderr)
        return 1
    input_path = Path(args.input_json)
    if not input_path.is_file():
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 1
    new_entries = json.loads(input_path.read_text())
    if not isinstance(new_entries, list):
        print("error: input JSON must be a list of source descriptors", file=sys.stderr)
        return 1

    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH
    index = load_index(index_path)
    index = add_entries(new_entries, index=index)
    last = index.pop("_last_add_summary", {})
    save_index(strip_internal_keys(index), path=index_path)
    print(f"Index updated: {index_path}")
    print(f"  added: {last.get('added', 0)}")
    print(f"  updated: {last.get('updated', 0)}")
    print(f"  total entries: {len(index['sources'])}")
    return 0


def cmd_index_list(args: argparse.Namespace) -> int:
    """Print a rollup of the index by audience, doc_type, location, status."""
    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH
    index = load_index(index_path)
    s = summary(index)
    json.dump(s, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_index_auto_skip(args: argparse.Namespace) -> int:
    """Auto-mark clearly-data entries (spreadsheets, csv uploads, tracker
    titles, etc.) as status=skipped with skip_reason."""
    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH
    index = load_index(index_path)
    counts = auto_skip_data_entries(index)
    save_index(strip_internal_keys(index), path=index_path)
    print(f"Auto-skip pass complete: {index_path}")
    print(f"  skipped now: {counts['skipped_now']}")
    print(f"  already skipped: {counts['already_skipped']}")
    print(f"  kept queued: {counts['kept']}")
    return 0


def cmd_index_classify(args: argparse.Namespace) -> int:
    """Run the LLM metadata classifier on queued entries to refine the
    heuristic-assigned audience + doc_type. By default only re-classifies
    entries with audience_confidence below `--threshold` (so high-confidence
    rule matches aren't redone). Use `--all` to redo every queued entry.

    Updates entries in place with:
        proposed_audience       <- classifier output
        audience_source         <- "classifier"
        audience_confidence     <- classifier confidence
        audience_alternates     <- classifier alternates (if any)
        proposed_doc_type       <- classifier output
        doc_type_source         <- "classifier"
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    threshold = float(args.threshold)
    workers = int(args.workers)
    limit = args.limit
    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH

    index = load_index(index_path)
    # Pick targets
    targets: list[dict[str, Any]] = []
    for s in index["sources"]:
        if s.get("status") != "queued":
            continue
        if not args.all and (s.get("audience_confidence") or 0.0) >= threshold:
            continue
        targets.append(s)
    if limit:
        targets = targets[: int(limit)]

    if not targets:
        print("No entries match the filter — nothing to classify.")
        return 0

    ok, err = preflight_auth_check()
    if not ok:
        print(f"\nAborting before any classifier calls: {err}", file=sys.stderr)
        return 4

    print(f"Classifying {len(targets)} entries with {workers} workers...")

    llm = VoiceLLM()
    results: dict[str, tuple[Classification | None, str | None]] = {}

    def _one(entry: dict[str, Any]) -> tuple[str, Classification | None, str | None]:
        try:
            c = llm.classify_metadata(
                title=entry["title"],
                location=entry["location"],
                space=entry.get("space"),
            )
            return entry["id"], c, None
        except Exception as e:
            return entry["id"], None, f"{type(e).__name__}: {e}"

    n_ok = 0
    n_err = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, t) for t in targets]
        for i, fut in enumerate(as_completed(futures), start=1):
            eid, cls, err = fut.result()
            if err:
                n_err += 1
                print(f"  [{i}/{len(targets)}] {eid}: ERROR {err}", file=sys.stderr)
            else:
                n_ok += 1
                results[eid] = (cls, None)
            if i % 10 == 0:
                print(f"  ...{i}/{len(targets)} done", flush=True)

    # Apply updates
    by_id = {s["id"]: s for s in index["sources"]}
    for eid, (cls, _) in results.items():
        if cls is None:
            continue
        s = by_id[eid]
        s["proposed_audience"] = cls.audience
        s["audience_source"] = "classifier"
        s["audience_confidence"] = round(cls.audience_confidence, 2)
        if cls.audience_alternates:
            s["audience_alternates"] = cls.audience_alternates
        elif "audience_alternates" in s:
            # No alternates from classifier — clear the heuristic's alternates
            # to avoid stale data.
            del s["audience_alternates"]
        s["proposed_doc_type"] = cls.doc_type
        s["doc_type_source"] = "classifier"

    save_index(strip_internal_keys(index), path=index_path)
    print(f"\nClassified {n_ok} entries ({n_err} errors). Index saved.")
    return 0


def cmd_propose_batch(args: argparse.Namespace) -> int:
    """Run /voice-analyze propose on every queued entry that has fetchable content.

    Picks targets:
    - status == "queued"
    - has either a local path (location=local) or a cached content file at
      ~/.cache/ai-seal-tools/voice/content/<id>.md

    For each, calls run_propose with the index's `proposed_audience` and
    `proposed_doc_type` as overrides — we already classified by metadata, so
    those drive the proposal's audience/doc_type bucket. The content classifier
    still runs (its prediction is recorded separately so we can compare
    metadata-only vs full-content classification over time).

    Writes one proposal YAML per source. Updates index entries' status to
    "analyzed" and stamps source_hash + proposal_id.

    Run with --dry-run to see which entries would be processed.
    """
    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH
    index = load_index(index_path)

    targets = [s for s in index["sources"] if s.get("status") == "queued"]
    if args.audience:
        targets = [s for s in targets if s.get("proposed_audience") == args.audience]

    # Filter to entries with available content. Apply --limit AFTER content
    # filtering so the cap is on actually-processable docs, not raw queue
    # position. Otherwise --limit 5 might leave you with 0 fetchable if the
    # first 5 queued entries are all uncached.
    fetchable: list[tuple[dict[str, Any], str]] = []  # (entry, text)
    skipped_no_content: list[dict[str, Any]] = []
    for entry in targets:
        try:
            fc = fetch_content(entry)
            fetchable.append((entry, fc.text))
        except FetchUnavailable as e:
            skipped_no_content.append(entry)
    if args.limit:
        fetchable = fetchable[: int(args.limit)]

    print(f"Queued targets: {len(targets)}")
    print(f"  with content: {len(fetchable)}")
    print(f"  no content yet: {len(skipped_no_content)}")
    if skipped_no_content and args.verbose:
        for e in skipped_no_content[:20]:
            print(f"    - {e['id']}: {e['title'][:70]}")
        if len(skipped_no_content) > 20:
            print(f"    ... and {len(skipped_no_content) - 20} more")

    if args.dry_run:
        print("(dry run — no proposals written)")
        return 0
    if not fetchable:
        print("Nothing to process.")
        return 0

    ok, err = preflight_auth_check()
    if not ok:
        print(f"\nAborting before any propose runs: {err}", file=sys.stderr)
        return 4

    proposals_dir = Path(args.proposals_dir).expanduser() if args.proposals_dir else PROPOSALS_DIR
    nlp = load_nlp()
    lexicons = load_lexicons()
    llm = VoiceLLM()

    n_ok = 0
    n_descriptor_leak = 0
    n_other_err = 0
    for i, (entry, text) in enumerate(fetchable, start=1):
        eid = entry["id"]
        title_short = entry["title"][:60]
        try:
            text_norm = normalize_text(text)
            proposal = run_propose(
                text=text_norm,
                nlp=nlp,
                lexicons=lexicons,
                llm=llm,
                override_audience=entry.get("proposed_audience"),
                override_doc_type=entry.get("proposed_doc_type"),
            )
            path = write_proposal(proposal, proposals_dir)
            entry["status"] = "analyzed"
            entry["source_hash"] = proposal.source_hash
            entry["proposal_id"] = proposal.proposal_id
            n_flagged = sum(1 for e in proposal.candidate_exemplars if e.scrub_status == "flagged")
            print(f"  [{i}/{len(fetchable)}] {eid}: OK → {path.name}  ({len(proposal.candidate_exemplars)} ex, {n_flagged} flagged)  ({title_short})")
            n_ok += 1
            # Save index after each successful run so crashes don't lose progress
            save_index(strip_internal_keys(index), path=index_path)
        except DescriptorLeak as leak:
            n_descriptor_leak += 1
            print(f"  [{i}/{len(fetchable)}] {eid}: DESCRIPTOR LEAK ({len(leak.findings)} findings) — proposal NOT written  ({title_short})", file=sys.stderr)
            for f in leak.findings[:3]:
                print(f"      • [{f['rule']}] {f['snippet']!r} at {f['where']}", file=sys.stderr)
        except Exception as e:
            n_other_err += 1
            print(f"  [{i}/{len(fetchable)}] {eid}: ERROR {type(e).__name__}: {e}  ({title_short})", file=sys.stderr)

    print(f"\nDone. ok={n_ok}, descriptor_leaks={n_descriptor_leak}, other_errors={n_other_err}")
    print(f"Proposals at: {proposals_dir}")
    print(f"Next: /voice-review")
    return 0


def cmd_index_show(args: argparse.Namespace) -> int:
    """Print full entries (optionally filtered by audience/status/contribution)."""
    index_path = Path(args.index_path).expanduser() if args.index_path else INDEX_PATH
    index = load_index(index_path)
    out = []
    for s in index["sources"]:
        if args.audience and s.get("proposed_audience") != args.audience:
            continue
        if args.status and s.get("status") != args.status:
            continue
        if args.contribution_type and s.get("contribution_type") != args.contribution_type:
            continue
        out.append(s)
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ---------- arg parsing -----------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="Run the deterministic stats extractor")
    p_extract.add_argument("path", help="Path to the document to analyze")
    p_extract.set_defaults(func=cmd_extract)

    p_classify = sub.add_parser("classify", help="Classify a document's audience + doc_type")
    p_classify.add_argument("path", help="Path to the document to classify")
    p_classify.set_defaults(func=cmd_classify)

    p_propose = sub.add_parser("propose", help="Full analysis → write proposal YAML for /voice-review")
    p_propose.add_argument("path", help="Path to the document to analyze")
    p_propose.add_argument("--audience", help=f"Override classifier; one of {sorted(VALID_AUDIENCES)}")
    p_propose.add_argument("--doc-type", help=f"Override classifier; one of {sorted(VALID_DOC_TYPES)}")
    p_propose.add_argument("--source-type",
                           help="Provenance tag (gmail, slack, confluence, gdrive, other). Stored on the proposal + sources_seen record for later filtering by medium.")
    p_propose.add_argument("--source-ref",
                           help="External identifier for the source: URL, email thread id, Slack thread URL, Confluence page id, etc. Stored as opaque string for traceability.")
    p_propose.add_argument("--proposals-dir", help="Override the proposals output directory")
    p_propose.set_defaults(func=cmd_propose)

    p_batch = sub.add_parser("propose-batch", help="Run propose on every queued index entry that has fetchable content")
    p_batch.add_argument("--audience", help="Only process entries with this proposed_audience")
    p_batch.add_argument("--limit", type=int, help="Cap number of entries (for testing)")
    p_batch.add_argument("--dry-run", action="store_true", help="Show what would run, don't call LLMs")
    p_batch.add_argument("--verbose", action="store_true", help="Show titles of entries skipped due to missing content")
    p_batch.add_argument("--proposals-dir", help="Override the proposals output directory")
    p_batch.add_argument("--index-path", help="Override the index file location")
    p_batch.set_defaults(func=cmd_propose_batch)

    p_index = sub.add_parser("index", help="Manage the source index (discovery queue)")
    p_index_sub = p_index.add_subparsers(dest="index_cmd", required=True)

    p_add = p_index_sub.add_parser("add", help="Add entries from a JSON list")
    p_add.add_argument("--input-json", required=True, help="Path to JSON file with [{location, title, url|path, ...}, ...]")
    p_add.add_argument("--index-path", help="Override the index file location")
    p_add.set_defaults(func=cmd_index_add)

    p_list = p_index_sub.add_parser("list", help="Print rollup summary (audience/doc_type/location/status counts)")
    p_list.add_argument("--index-path", help="Override the index file location")
    p_list.set_defaults(func=cmd_index_list)

    p_skip = p_index_sub.add_parser("auto-skip", help="Auto-mark obvious data-only entries (sheets/uploads/trackers) as skipped")
    p_skip.add_argument("--index-path", help="Override the index file location")
    p_skip.set_defaults(func=cmd_index_auto_skip)

    p_class = p_index_sub.add_parser("classify", help="Run the LLM metadata classifier on queued entries to refine heuristic guesses")
    p_class.add_argument("--threshold", default=0.7, help="Only re-classify entries with audience_confidence below this (default 0.7)")
    p_class.add_argument("--all", action="store_true", help="Re-classify every queued entry regardless of current confidence")
    p_class.add_argument("--workers", default=6, help="Concurrent worker count (default 6)")
    p_class.add_argument("--limit", default=None, help="Limit number of entries (for testing)")
    p_class.add_argument("--index-path", help="Override the index file location")
    p_class.set_defaults(func=cmd_index_classify)

    p_show = p_index_sub.add_parser("show", help="Print full entries (optionally filtered)")
    p_show.add_argument("--audience", help="Filter by proposed_audience")
    p_show.add_argument("--status", help="Filter by status")
    p_show.add_argument("--contribution-type", help="Filter by contribution_type (authored|contributed|unknown)")
    p_show.add_argument("--index-path", help="Override the index file location")
    p_show.set_defaults(func=cmd_index_show)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
