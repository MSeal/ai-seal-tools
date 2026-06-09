"""Claude API calls for the voice-analyze skill.

Three operations:
  1. classify(text) — audience tag + doc_type + axis estimates.
  2. extract_descriptors(text) — qualitative descriptors with strict anti-leak.
  3. generate_exemplars(text, patterns) — synthetic exemplars demonstrating
     patterns, again with strict anti-leak.

Each operation has a fixed system prompt (cached via cache_control=ephemeral)
and a variable user message. All outputs are JSON; we parse and validate
shape before returning typed dataclasses.

The caller (analyzer.py) is responsible for running the scrub validator on
descriptor and exemplar outputs against the source text. This module does
NOT scrub — its job is to produce + parse the LLM response; the scrub guard
sits at the orchestration boundary.

For tests, inject a mock `client` into VoiceLLM.__init__ that returns canned
responses; the prompts and parsing can be tested without real API calls.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic

from lexicons import VALID_AUDIENCES, VALID_DOC_TYPES

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
# Per-call timeout in seconds. Without this the SDK can block indefinitely
# when the underlying credential expires mid-run (the request hangs on the
# socket waiting for a response that won't come). 120s covers normal
# Sonnet response times with margin.
DEFAULT_TIMEOUT = 120.0


def _make_default_client():
    """Pick the right Anthropic client class based on environment.

    Users running under Claude Code on Vertex have CLAUDE_CODE_USE_VERTEX set
    along with ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION (or
    GOOGLE_CLOUD_REGION). The SDK's AnthropicVertex picks those up.
    Otherwise we use the standard Anthropic client which reads ANTHROPIC_API_KEY.
    """
    if os.environ.get("CLAUDE_CODE_USE_VERTEX"):
        return anthropic.AnthropicVertex()
    return anthropic.Anthropic()


def preflight_auth_check() -> tuple[bool, str | None]:
    """Verify the auth path is usable BEFORE starting a long batch.

    On Vertex this means refreshing the ADC token; on standard Anthropic it
    means a non-empty ANTHROPIC_API_KEY. We don't actually make an LLM call
    — just confirm the credentials would resolve.

    Returns (ok, error_message).
    """
    if os.environ.get("CLAUDE_CODE_USE_VERTEX"):
        try:
            import google.auth
            from google.auth.transport.requests import Request
            creds, _ = google.auth.default()
            creds.refresh(Request())
            return True, None
        except Exception as e:
            return False, (
                f"Vertex auth pre-flight failed: {type(e).__name__}: {e}\n"
                f"Run: gcloud auth application-default login"
            )
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return False, "ANTHROPIC_API_KEY is not set."
    return True, None


# ---------- Prompts ----------------------------------------------------------

CLASSIFY_METADATA_SYSTEM = """You are categorizing documents for a writing-style profile by METADATA only (title, location, space, optional short snippet). Full document body is not available.

Make your best inference based on the metadata. Be honest about confidence — title-only signals are weaker than full prose. If the title is generic or ambiguous, set confidence below 0.6.

1. The intended **audience** — pick ONE tag:
   - technical_peer: Design docs, RFCs, technical messages to engineering peers. The most common bucket for engineer-authored Confluence pages.
   - leadership: Status updates, project pitches, escalations, roadmap docs, OKR/business goals, headcount/structure discussions
   - direct_report: 1:1 prep, feedback, coaching messages
   - cross_functional: Specs, primers, partnership asks, persona docs aimed at PM/design/non-engineering partners
   - external_public: Blog posts, talk abstracts, conference outlines, public READMEs, PR/FAQ templates
   - casual: DMs to friends, casual Slack, social messages
   - self_notes: Journals, todos, personal scratchpads

2. The **doc_type**:
   - polished: complete prose intended as a finished artifact (most Confluence pages)
   - draft: in-progress prose, often in personal space or marked [WIP]
   - outline: bullets / fragments — outline → expand-later (talk outlines, scratchpads, "draft outline" titles)

3. **axis_estimates** (0-1 scales): formality, technical_density, brevity, warmth.

Hints for metadata-only:
- "Design Note:", "Decision Log:", "Architecture:", "Technical State", "Security Review" → almost always technical_peer/polished
- "PR/FAQ Template", "Talk", "Bangalore <year>", "Conference" → external_public; titles with "Outline" → outline doc_type
- "Leadership Sync", "Go/No-Go", "Operational Suggestions", "Team Structure", "Roadmap", "OKR", "Business Goals", "NRR", "Quarterly Plan" → leadership
- "Partnership Asks", "Persona", "Primer", "Requirements" → cross_functional
- Personal Confluence space (key starts with `~`) → skew toward draft when title doesn't strongly indicate polished
- WIP markers / "Scratchpad" / "Brain dump" → draft or outline
- Jira epics with short descriptions → low confidence; default to technical_peer
- gdrive_sheets / "Burndown" / "Stats" / "Tracker" / "Tickets" → these are usually data, not voice corpus material — flag in reasoning if you suspect that

Output JSON only — no prose around it, no markdown fences. Schema:

{
  "audience": "<one of the seven tags>",
  "audience_confidence": <0-1>,
  "audience_alternates": [<list of plausible alternates>],
  "doc_type": "polished" | "draft" | "outline",
  "doc_type_confidence": <0-1>,
  "axis_estimates": {
    "formality": <0-1>,
    "technical_density": <0-1>,
    "brevity": <0-1>,
    "warmth": <0-1>
  },
  "reasoning": "<1-2 sentence justification — call out if title is too generic to be confident>"
}
"""


CLASSIFY_SYSTEM = """You are a stylometric classifier. Given a writing sample, identify:

1. The intended **audience** — pick ONE tag from this fixed set:
   - technical_peer: Design docs, RFCs, technical messages to engineering peers
   - leadership: Status updates, project pitches, escalations to managers/execs
   - direct_report: 1:1 prep, feedback, coaching messages to direct reports
   - cross_functional: Specs, explainers for PM/design/non-engineering partners
   - external_public: Blog posts, talk abstracts, public READMEs
   - casual: DMs to friends, casual Slack, social messages
   - self_notes: Journals, todos, scratch — unfiltered private voice

2. The **doc_type**:
   - polished: complete prose intended as a finished artifact
   - draft: in-progress prose with placeholders or rough edges
   - outline: mostly bullets / short fragments / structure-first not prose-first

3. **axis_estimates** — the document's position on four 0-1 scales:
   - formality: 0=very casual, 1=very formal
   - technical_density: 0=layperson, 1=highly technical
   - brevity: 0=leisurely, 1=terse
   - warmth: 0=clinical, 1=warm/personal

Decide based on structural and tonal cues. Topic content is not relevant —
focus on register, audience-signaling phrases, sentence shape, and structure.

Output JSON only — no prose around it, no markdown fences. Schema:

{
  "audience": "<one of the seven tags>",
  "audience_confidence": <0-1>,
  "audience_alternates": [<list of plausible alternates, possibly empty>],
  "doc_type": "polished" | "draft" | "outline",
  "doc_type_confidence": <0-1>,
  "axis_estimates": {
    "formality": <0-1>,
    "technical_density": <0-1>,
    "brevity": <0-1>,
    "warmth": <0-1>
  },
  "reasoning": "<1-2 sentence justification>"
}
"""


DESCRIBE_SYSTEM = """You are extracting a writing-style profile from a document. Describe HOW the author writes, NOT WHAT they write about. The user wants a reusable voice profile.

**CRITICAL ANTI-LEAK RULES** — your output is stored verbatim in a profile that must not contain content from this specific document:

1. NEVER quote or paraphrase phrases longer than 5 words from the document.
2. NEVER use proper nouns from the document: no names of people, companies, products, projects, places, files, services, technologies (including programming languages, libraries, frameworks, and product/feature names if domain-specific). Generic categories are OK ("a database", "a meeting").
3. NEVER reference specific topics, decisions, numbers, dates, identifiers, or facts from the document. Describe patterns abstractly.
4. If giving a concrete example, use placeholder content like "X", "Y", "Alice", "Bob", "the team", "the feature", "the service".
5. NEVER include emails, URLs, @-handles, or hash-like identifiers.
6. Acronyms in widespread general use (HTTP, API, SDK, OS, CLI, UI, SQL) are OK; project-specific or domain-specific acronyms are NOT.
7. NEVER repeat verbatim section headers, label names, or list markers from the document (e.g. if the doc has a "Constraints:" section, do NOT mention "Constraints" — say "named-constraint sections" or "labelled sub-sections").
8. NEVER quote phrases of 4+ consecutive words from the document, even when describing a "pattern".

Output JSON only — no prose around it, no markdown fences. Schema:

{
  "voice_summary": "<2-3 sentences capturing overall tone, register, energy>",
  "rhetorical_moves": ["<pattern>", ...],
  "tics": ["<pattern>", ...],
  "structural_habits": ["<pattern>", ...],
  "openings_inventory": ["<pattern>", ...],
  "closings_inventory": ["<pattern>", ...],
  "transition_style": "<prose description>",
  "humor_register": "<prose description, or 'none observed'>",
  "self_reference_behavior": "<prose description>",
  "what_to_avoid": ["<un-like-the-writer pattern>", ...]
}

Field guidance:

- voice_summary: 2-3 sentences. Tone, register, energy. Like a colleague describing how the writer comes across.
- rhetorical_moves: structural argument moves. Examples: "States the tradeoff before stating the recommendation", "Names the goal then immediately lists the constraints".
- tics: small habits — hedge patterns, transition phrases, sentence-shape preferences. Describe the habit; don't quote it. Example: "Uses soft-recommendation markers ('seems', 'might') frequently before assertions."
- structural_habits: macro-structure patterns. Examples: "Opens with context, narrows to specific proposal, ends with risks".
- openings_inventory: how the writer starts documents. Examples: "Tradeoff-first opener", "Goal-statement opener", "Problem-statement opener".
- closings_inventory: how the writer ends documents. Examples: "Risk-list closer", "Open-question redirect", "Call-to-action closer".
- transition_style: how the writer moves between sections/paragraphs. Prose.
- humor_register: if humor appears, where and how it's signaled. Often "none observed" for technical writing.
- self_reference_behavior: how the writer refers to themselves (I, we, the author, none).
- what_to_avoid: patterns that would feel UN-LIKE this writer. Voice-specific anti-patterns. Example: "Avoid em-dashes for asides — writer uses commas/parentheses instead."

Aim for 3-7 items in each list field. Be specific but always content-free.
"""


EXEMPLIFY_SYSTEM = """You are creating SYNTHETIC EXAMPLES of writing patterns. The patterns come from a real document, but each example must contain NO content from that document — only placeholder content that demonstrates the pattern's structure and rhythm.

**ANTI-LEAK RULES** (same as descriptor extraction — these are stored verbatim):

1. NEVER quote or paraphrase phrases longer than 5 words from the source document.
2. NEVER use proper nouns from the source (names, companies, products, projects, technologies).
3. USE placeholders: "Alice", "Bob", "Carol", "X", "Y", "Z", "the team", "the feature", "the service", "tool A", "system B".
4. Generic categories are OK: "a database", "an API", "a deploy", "an incident".
5. Keep each example short — 1 to 3 sentences.

For each pattern below, generate ONE synthetic example demonstrating it. Capture the WRITER'S RHYTHM and STRUCTURE without using their content.

Output JSON only — no prose around it, no markdown fences. Schema:

{
  "exemplars": [
    {
      "pattern_id": "<short_slug, lowercase, underscores>",
      "pattern": "<short description of the pattern>",
      "synthetic": "<the example, 1-3 sentences>",
      "when_to_use": "<one sentence on when this pattern fits>"
    },
    ...
  ]
}
"""


# ---------- Data classes ----------------------------------------------------

@dataclass
class Classification:
    audience: str
    audience_confidence: float
    audience_alternates: list[str]
    doc_type: str
    doc_type_confidence: float
    axis_estimates: dict[str, float]
    reasoning: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "audience_confidence": self.audience_confidence,
            "audience_alternates": self.audience_alternates,
            "doc_type": self.doc_type,
            "doc_type_confidence": self.doc_type_confidence,
            "axis_estimates": self.axis_estimates,
            "reasoning": self.reasoning,
        }


@dataclass
class CandidateExemplar:
    pattern_id: str
    pattern: str
    synthetic: str
    when_to_use: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "pattern": self.pattern,
            "synthetic": self.synthetic,
            "when_to_use": self.when_to_use,
        }


# ---------- JSON parsing -----------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from a Claude response, stripping optional markdown fences."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM response was not valid JSON. Error: {e}. "
            f"Response (first 500 chars): {text[:500]!r}"
        ) from e


# ---------- VoiceLLM ---------------------------------------------------------

class VoiceLLM:
    """Wrapper around the three Claude calls. Inject a mock client for testing."""

    def __init__(
        self,
        client=None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.client = client if client is not None else _make_default_client()
        self.model = model
        self.max_tokens = max_tokens

    def _call(self, system_prompt: str, user_content: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            timeout=DEFAULT_TIMEOUT,
        )
        # First content block is the text response in standard mode
        return response.content[0].text

    def classify_metadata(
        self,
        title: str,
        location: str,
        space: str | None = None,
        snippet: str | None = None,
    ) -> Classification:
        """Classify by metadata only (no document body). Cheaper than classify()
        and useful for triaging a large discovery queue before deciding which
        documents to fetch and analyze in full."""
        user_content_lines = [
            f"Title: {title}",
            f"Location: {location}",
        ]
        if space:
            user_content_lines.append(f"Space: {space}")
        if snippet:
            user_content_lines.append(f"Snippet: {snippet[:500]}")
        user_content = "\n".join(user_content_lines)
        raw = self._call(CLASSIFY_METADATA_SYSTEM, user_content)
        return self._parse_classification(raw)

    def classify(self, text: str) -> Classification:
        raw = self._call(CLASSIFY_SYSTEM, text)
        return self._parse_classification(raw)

    def _parse_classification(self, raw: str) -> Classification:
        """Parse + validate either metadata-mode or full-mode classifier output."""
        data = parse_json_response(raw)
        _require_keys(data, [
            "audience", "audience_confidence", "audience_alternates",
            "doc_type", "doc_type_confidence", "axis_estimates", "reasoning",
        ], "classifier")
        if data["audience"] not in VALID_AUDIENCES:
            raise ValueError(
                f"Classifier returned invalid audience {data['audience']!r}; "
                f"valid: {sorted(VALID_AUDIENCES)}"
            )
        if data["doc_type"] not in VALID_DOC_TYPES:
            raise ValueError(
                f"Classifier returned invalid doc_type {data['doc_type']!r}; "
                f"valid: {sorted(VALID_DOC_TYPES)}"
            )
        axes = data["axis_estimates"]
        _require_keys(axes, ["formality", "technical_density", "brevity", "warmth"], "axis_estimates")
        return Classification(
            audience=data["audience"],
            audience_confidence=float(data["audience_confidence"]),
            audience_alternates=list(data["audience_alternates"]),
            doc_type=data["doc_type"],
            doc_type_confidence=float(data["doc_type_confidence"]),
            axis_estimates={k: float(axes[k]) for k in ("formality", "technical_density", "brevity", "warmth")},
            reasoning=str(data["reasoning"]),
        )

    def extract_descriptors(self, text: str) -> dict[str, Any]:
        raw = self._call(DESCRIBE_SYSTEM, text)
        data = parse_json_response(raw)
        _require_keys(data, [
            "voice_summary", "rhetorical_moves", "tics", "structural_habits",
            "openings_inventory", "closings_inventory", "transition_style",
            "humor_register", "self_reference_behavior", "what_to_avoid",
        ], "descriptors")
        return data

    def generate_exemplars(self, text: str, patterns: list[str]) -> list[CandidateExemplar]:
        if not patterns:
            return []
        user_content = (
            "Source document (for rhythm/structure reference only — do not quote):\n"
            f"---\n{text}\n---\n\n"
            "Patterns to exemplify:\n"
            + "\n".join(f"- {p}" for p in patterns)
        )
        raw = self._call(EXEMPLIFY_SYSTEM, user_content)
        data = parse_json_response(raw)
        _require_keys(data, ["exemplars"], "exemplars")
        out: list[CandidateExemplar] = []
        for item in data["exemplars"]:
            _require_keys(item, ["pattern_id", "pattern", "synthetic", "when_to_use"], "exemplar")
            out.append(CandidateExemplar(
                pattern_id=str(item["pattern_id"]),
                pattern=str(item["pattern"]),
                synthetic=str(item["synthetic"]),
                when_to_use=str(item["when_to_use"]),
            ))
        return out


def _require_keys(data: dict, required: list[str], what: str) -> None:
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"{what} response missing required keys: {missing}")
