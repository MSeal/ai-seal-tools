---
name: voice-analyze
description: Analyze a writing sample → propose updates to the user's voice profile. Three sub-commands. `extract <doc>` runs the deterministic stylometric stats extractor only (sentence/paragraph distributions, function-word frequencies, pronoun ratios, hedge/booster/stance rates, passive voice, lexical diversity, lexical complexity incl. concreteness when installed, readability, POS-based sentence openers, connective preferences, bullet density). `classify <doc>` runs the LLM audience + doc_type classifier. `propose <doc>` runs the full pipeline: extracts stats + LLM-generated paraphrased descriptors + LLM-generated synthetic exemplars, scrub-validates every LLM output against the source, writes a proposal yaml. SEEDS (raw source documents) stay in machine-local zones — `~/.cache/ai-seal-tools/voice/content/<id>.md` for fetched bodies, `scratch/` for review drafts — and never enter the profile. EXEMPLARS (synthetic placeholder-content patterns) and PARAPHRASED DESCRIPTORS are the only outputs that persist; both go through scrub validation and the human-review gate (/voice-review). Live profile at ~/.config/ai-seal-tools/voice/profile.yaml is personal-Drive-synced and contains no source text, titles, or third-party identifiers — only abstracted style data.
---

# Voice Analyze

Status: v1 implemented. Schema, templates, symlink layout, deterministic stats extractor, LLM classifier + descriptor extractor + exemplar generator, and scrub validator are all in place. `/voice-review` and `/voice-write` are planned next.

## What this skill does

Three sub-commands invokable as `analyzer.py <cmd> <doc>`:

**`extract <doc>`** — runs the deterministic stats extractor and prints JSON. No LLM calls. Useful for inspecting raw stylometric signal on a document.

**`classify <doc>`** — runs the Claude classifier and prints the audience + doc_type + axis estimates. One API call.

**`propose <doc> [--audience <tag>] [--doc-type <type>] [--proposals-dir <path>]`** — full pipeline:

1. Read document, normalize text, compute SHA-256 hash.
2. Run the LLM classifier → `Classification` (audience, doc_type, axis estimates, confidence).
3. Run the LLM descriptor extractor → 10 qualitative fields (voice_summary, rhetorical_moves, tics, structural_habits, openings/closings inventories, transition_style, humor_register, self_reference_behavior, what_to_avoid).
4. **Scrub descriptors** against the source text. Any leak (>5-word substring overlap, proper noun, email/URL/handle, hash, long digit run) halts the run — no proposal is written. The user sees the specific findings.
5. Pick patterns (rhetorical_moves + opening/closing inventories, capped at 8) and run the LLM exemplar generator → synthetic placeholder-content examples.
6. **Scrub each exemplar** against the source. Flagged exemplars stay in the proposal with `scrub_status: flagged` so the reviewer sees them, rather than halting the run.
7. Run the deterministic stats extractor → quantitative stats block.
8. Write the proposal to `~/.config/ai-seal-tools/voice/proposals/<proposal_id>.yaml`. The proposal validates against `proposal_v1.json` before being written.

Nothing enters `profile.yaml` until `/voice-review` walks the proposal and the user accepts each piece (exemplars require per-item accept).

The `--audience` and `--doc-type` flags override the classifier's output but the classifier still runs (its prediction is recorded alongside the override so we can evaluate classifier accuracy over time).

## Where state lives

Created by `utils/install_skills.py` from `links.yaml`:

| Path | Purpose | Drive-backed |
|---|---|---|
| `~/.config/ai-seal-tools/voice/profile.yaml` | The live voice profile | Yes |
| `~/.config/ai-seal-tools/voice/sources_seen.yaml` | Hash-keyed log of analyzed sources | Yes |
| `~/.config/ai-seal-tools/voice/proposals/` | Pending proposal files awaiting review | No (machine-local) |
| `~/.config/ai-seal-tools/voice/archive/` | Accepted/rejected proposal log | Yes |

The schema files and templates are checked-in under this skill at `schemas/` and `templates/`.

## Schema versioning

`schema.py` exposes `LATEST_PROFILE_SCHEMA` and `LATEST_SOURCES_SEEN_SCHEMA`. On load:

- Equal version → validate and proceed.
- Older → run migrations sequentially (`migrations/<kind>_v<N>_to_v<N+1>.py`), log to `merge_history`, write back.
- Newer → halt: "Profile uses schema v<N>; code supports up to v<M>. Update the voice-analyze skill."

Every schema uses `additionalProperties: false`. Adding a field requires a schema bump + a migration; the validator catches drift loudly.

## Sister skills (planned, not yet built)

- `voice-review` — walk a proposal file, accept/reject each piece, merge into the live profile.
- `voice-write` — produce a draft in the user's voice, given an intent + audience + doc_type.

## Known iteration targets (observed during seed-doc validation)

These are not bugs but rough edges seen on the first real `propose` run. The
evaluation harness (planned) should make these inspectable across many docs
so we tune the prompts/scrub from real data rather than guesses:

- **LLM-generated dates/quarters in synthetic exemplars** — the LLM may
  invent placeholder dates ("On March 12th") or quarter references ("in Q3").
  Anti-leak rules don't forbid them currently. Options: tighten the exemplar
  prompt to disallow specific dates, or add a date-pattern scrub rule.
- **Paraphrased numerical claims** — "tens of thousands of active users"
  passed scrub but is structurally close to a number from the source
  ("35-50k monthly users"). Either explicitly prompt for placeholder
  numbers ("N users", "<scale> users") or add fuzzy-number similarity to
  scrub.
- **Quality of `what_to_avoid` items** — first pass produced solid items but
  no derived-from-stats links (e.g. "Em-dash rate should stay near 0/100w,
  not Claude's 3/100w"). The merger that builds the live profile from
  proposals is the right place to inject these, not the analyzer.
