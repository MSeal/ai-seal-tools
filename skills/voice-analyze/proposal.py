"""Proposal file format and orchestration helpers.

A *proposal* is the analyzer's output for a single document: classification,
extracted stats, LLM-generated descriptors, candidate exemplars, and a record
of any scrub-validator findings. Proposals live machine-local in
`~/.config/ai-seal-tools/voice/proposals/` and are consumed by /voice-review
for the mandatory human-accept step before anything enters the live profile.

Proposals are not Drive-synced — see links.yaml's explicit comment on why.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import schema as voice_schema
from llm import CandidateExemplar, Classification
from scrub import ScrubFinding


PROPOSALS_DIR = Path.home() / ".config" / "ai-seal-tools" / "voice" / "proposals"


# ---------- Data classes ----------------------------------------------------

@dataclass
class ExemplarWithScrub:
    """A candidate exemplar plus its scrub result."""
    id: str
    pattern_id: str
    pattern: str
    synthetic: str
    when_to_use: str
    source_hash: str
    scrub_status: str  # "passed" | "flagged"
    scrub_findings: list[ScrubFinding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "pattern_id": self.pattern_id,
            "pattern": self.pattern,
            "synthetic": self.synthetic,
            "when_to_use": self.when_to_use,
            "source_hash": self.source_hash,
            "scrub_status": self.scrub_status,
        }
        if self.scrub_findings:
            out["scrub_findings"] = [_finding_as_dict(f, where=f"exemplar:{self.id}") for f in self.scrub_findings]
        return out


@dataclass
class Proposal:
    proposal_id: str
    created_at: str
    source_hash: str
    source_word_count: int
    classification: Classification
    proposed_stats: dict[str, Any]
    proposed_descriptors: dict[str, Any]
    candidate_exemplars: list[ExemplarWithScrub]
    scrub_findings: list[dict[str, Any]] = field(default_factory=list)
    review_status: str = "pending"
    notes: str = ""
    override_audience: str | None = None
    override_doc_type: str | None = None
    # Provenance — what kind of source this came from (gmail/slack/confluence/
    # gdrive/other) and an optional external identifier (URL, thread id, page
    # id). Lets later consumers filter exemplars by medium and trace back to
    # the original artifact without re-storing any of its content.
    source_type: str | None = None
    source_ref: str | None = None
    # Authorship signal — "full" if the user wrote the entire source,
    # "partial" if only some of it. Partial sources contribute only
    # exemplars (which the reviewer accepts per-item); their stats and
    # descriptors are skipped on merge because the aggregates would mix
    # in other contributors' voice.
    authorship: str = "full"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "created_at": self.created_at,
            "source_hash": self.source_hash,
            "source_word_count": self.source_word_count,
            "classification": self.classification.as_dict(),
            "proposed_stats": self.proposed_stats,
            "proposed_descriptors": self.proposed_descriptors,
            "candidate_exemplars": [e.as_dict() for e in self.candidate_exemplars],
            "scrub_findings": self.scrub_findings,
            "review_status": self.review_status,
        }
        if self.notes:
            out["notes"] = self.notes
        if self.override_audience is not None:
            out["override_audience"] = self.override_audience
        if self.override_doc_type is not None:
            out["override_doc_type"] = self.override_doc_type
        if self.source_type is not None:
            out["source_type"] = self.source_type
        if self.source_ref is not None:
            out["source_ref"] = self.source_ref
        # Only serialize authorship when it's non-default ("partial") so
        # the schema stays clean for the default case.
        if self.authorship and self.authorship != "full":
            out["authorship"] = self.authorship
        return out


# ---------- Helpers ---------------------------------------------------------

def _finding_as_dict(finding: ScrubFinding, where: str) -> dict[str, Any]:
    return {
        "rule": finding.rule,
        "snippet": finding.snippet,
        "detail": finding.detail,
        "where": where,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_proposal_id(source_hash: str) -> str:
    """Stable-ish proposal id: timestamp + short hash prefix."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"prop_{ts}_{source_hash[:8]}"


def new_exemplar_id(source_hash: str, index: int) -> str:
    """Per-doc unique, also somewhat globally distinct via hash prefix."""
    return f"ex_{source_hash[:6]}_{index:03d}"


# ---------- Read / write ----------------------------------------------------

def write_proposal(proposal: Proposal, dest_dir: Path = PROPOSALS_DIR) -> Path:
    """Validate against schema, then write to <dest_dir>/<proposal_id>.yaml."""
    data = proposal.as_dict()
    voice_schema.validate("proposal", data)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{proposal.proposal_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def read_proposal(path: Path) -> dict[str, Any]:
    """Load a proposal file. Returns the raw dict (reviewer logic does its own
    parsing into in-memory structures). Validates schema."""
    data = yaml.safe_load(path.read_text())
    voice_schema.validate("proposal", data)
    return data


def list_pending_proposals(dest_dir: Path = PROPOSALS_DIR) -> list[Path]:
    """List proposal files with review_status='pending'. Sorted oldest-first."""
    if not dest_dir.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(dest_dir.glob("prop_*.yaml")):
        try:
            data = yaml.safe_load(p.read_text())
            if (data or {}).get("review_status") == "pending":
                out.append(p)
        except (yaml.YAMLError, OSError):
            continue
    return out
