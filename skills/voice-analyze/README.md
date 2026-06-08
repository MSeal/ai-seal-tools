# Voice Profile System — Overview

A personal stylometric voice profile, layered by audience and doc-type,
that learns from your existing writing without storing the source content.

## Seeds vs exemplars (the core distinction)

The pipeline has two kinds of artifact, and treating them the same is the
fastest way to leak source content:

| | **Seeds** | **Exemplars** |
|---|---|---|
| What | Raw source documents | Synthetic placeholder-content patterns |
| Lifecycle | Discovered → fetched → analyzed (then discardable) | Reviewed once → kept forever in the profile |
| Contains | Your actual prose, real names, real topics | "Alice", "Bob", "X vs Y", "the team", "tool A" — never source content |
| Used by | `/voice-analyze` as input | The future `/voice-write` as reference |
| Volume | Hundreds; we analyze a subset | ~5-8 per accepted proposal, all human-reviewed |

The whole pipeline is built around **never letting seed content cross into
the exemplar / profile zone**.

## Storage zones

| Zone | What lives there | Safe to commit? |
|---|---|---|
| **Repo (tracked)** | Code, schemas, prompts, design docs in this directory + sister skills | ✓ |
| **Personal Drive (synced)** | `~/.config/ai-seal-tools/voice/profile.yaml` (paraphrased descriptors + synthetic exemplars), `sources_seen.yaml` (hashes only), `source_index.yaml` (titles + URLs — your discovery layer), `archive/` (reviewed proposals) | ✓ Drive-personal only — NEVER commit to repo |
| **Machine-local only** | `scratch/voice-corpus/*` (review drafts, parser outputs), `scratch/voice-seed/*` (raw seeds), `~/.config/.../proposals/` (pre-review), `~/.cache/.../content/` (fetched seed bodies) | ✗ Anything with source prose or third-party names |

The hard rule: **anything containing actual document text stays in
machine-local zones**. The personal Drive is OK for titles + URLs + your own
profile but not for raw prose.

## Three skills

| Skill | What it does |
|---|---|
| `/voice-analyze` (this directory) | Discovery, classification, stat extraction, descriptor extraction, exemplar generation, scrub validation, proposal writing |
| `/voice-review` (sister directory) | Interactive walk-through of pending proposals + merge into the live profile |
| `/voice-write` (planned) | Draft new prose in your voice, conditioned by audience + doc_type + optional axis nudges |

## Documents in this directory

| File | Purpose |
|---|---|
| `APPROACH.md` | The overall design: pipeline, audience/doc-type model, privacy guarantees, profile merge math, schema versioning |
| `METRICS.md` | Every metric the deterministic extractor produces and what it measures |
| `SETUP.md` | One-time machine setup: spaCy model install, optional concreteness/Dale-Chall lexicons, Vertex/Anthropic auth |
| `SKILL.md` | The `/voice-analyze` slash-command definition |
| Sister skill `voice-review/REVIEW_PROCESS.md` | What `/voice-review` does, the per-item exemplar gate, lifecycle |

## Quick start

After running `utils/install_skills.py` once:

```bash
# 1. Discover documents into the index
#    (we already seeded the index with Glean + local docs)
UV_NO_CONFIG=1 uv run skills/voice-analyze/analyzer.py index list

# 2. Edit the review files to fix audience/doc_type guesses
$EDITOR scratch/voice-corpus/review.md
$EDITOR scratch/voice-corpus/review_skipped.md
UV_NO_CONFIG=1 uv run scratch/voice-corpus/apply_review.py

# 3. Fetch document content (one-time, content cached locally)
#    For Confluence: I'll fetch via MCP into ~/.cache/ai-seal-tools/voice/content/
#    For Drive: TODO (needs Drive OAuth scope)

# 4. Run propose-batch over everything queued with cached content
UV_NO_CONFIG=1 uv run skills/voice-analyze/analyzer.py propose-batch

# 5. Walk the proposals, accept each piece, merge into profile
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk
```

## Key files (read in order if you're new)

1. **`APPROACH.md`** — start here. Explains why this exists and how the
   pieces fit together.
2. **`METRICS.md`** — what we actually measure and why each metric matters
   for voice.
3. **`../voice-review/REVIEW_PROCESS.md`** — the manual gate that keeps
   the LLM honest.
4. **`SKILL.md`** — concrete invocation for `/voice-analyze`.
5. **`SETUP.md`** — one-time install requirements.

## State of the system

| Component | Status |
|---|---|
| Schema (v1) for profile, sources_seen, proposal, source_index | Done |
| Deterministic stats extractor (16 metric blocks) | Done |
| LLM classifier (metadata + full-content modes) | Done |
| LLM descriptor extractor with anti-leak prompt | Done |
| LLM exemplar generator with placeholder-content rule | Done |
| Scrub validator (n-gram overlap, proper nouns with sentence-initial detection, identifiers) | Done |
| Source index with audience/doc_type heuristic | Done |
| Content fetcher (local files + cache pattern for remote) | Done |
| Propose-batch with preflight auth + per-source crash recovery | Done |
| Voice-review with mandatory per-item exemplar gate | Done |
| Profile merge logic with weighted-average stats | Done |
| Test coverage | 700+ tests across all modules |
| Drive OAuth scope for fetching Google Docs/Slides | **Not yet** |
| `voice-write` skill for draft generation | **Planned** |
| Evaluation harness | **Planned** |

## What the system does NOT do (yet)

- Fetch Google Docs/Slides/Sheets content (requires extending OAuth scope)
- Fetch from external sources beyond Confluence + local
- Use the profile at draft-time (voice-write skill is planned)
- Evaluate profile quality automatically (manual review only)
- Multi-author profiles (single-author design)
