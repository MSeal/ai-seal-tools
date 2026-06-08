# Voice Profile — Approach

## Goal

Build a reusable, privacy-preserving model of a single writer's voice that
can be:
1. **Trained incrementally** from documents (proposals reviewed manually
   before anything enters the live profile)
2. **Conditioned by audience and doc-type** (writing for engineering peers
   reads differently than writing for leadership)
3. **Preserved across machines and over time** without storing the source
   documents themselves
4. **Used at write-time** to draft new prose in the writer's voice (planned;
   the `voice-write` skill)

The system is intentionally personal-scale (one author, opinionated defaults)
rather than a generic multi-tenant service. It exists to fight against the
default "AI voice" — em-dash overuse, performative enthusiasm, the
**bold-term:**-explanation bullet, signposting transitions — by recording
**what the writer's actual habits look like** and using those at draft time.

## Pipeline

```
┌─────────────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────────┐
│ Source document │ → │ /voice-      │ → │ proposal    │ → │ /voice-      │
│ (Confluence,    │   │   analyze    │   │  YAML       │   │   review     │
│  Drive, local…) │   │              │   │ (machine-   │   │ (manual      │
│                 │   │ 3 LLM calls  │   │  local)     │   │  accept)     │
└─────────────────┘   │ + scrub      │   └─────────────┘   └──────┬───────┘
                      └──────────────┘                            │
                                                                  ▼
                                                          ┌──────────────┐
                                                          │ profile.yaml │
                                                          │ (Drive-      │
                                                          │  synced)     │
                                                          └──────────────┘
```

Five distinct components:

1. **Source index** (`source_index.yaml`) — discovery queue. Lists every
   document we might analyze, with audience/doc_type heuristics and
   contribution_type flags. Edited via the review-file workflow before
   batches run.

2. **Content fetcher** (`fetcher.py`) — reads local files directly,
   pre-cached Confluence/Drive content from
   `~/.cache/ai-seal-tools/voice/content/<source_id>.md` otherwise. Cache
   is populated out-of-band (via MCP tools, REST APIs, etc.).

3. **Analyzer** (`/voice-analyze`) — runs three LLM calls + a deterministic
   stats extractor + a scrub validator. Produces a proposal YAML.

4. **Reviewer** (`/voice-review`) — walks each proposal interactively.
   Mandatory per-item accept for exemplars (the highest-leak risk content).

5. **Profile** (`profile.yaml`) — the live, layered voice profile.
   Audience-bucketed and doc-type-subdivided.

## What gets stored vs what doesn't

**The profile NEVER stores**:
- Source document text, titles, file paths, or URLs
- Third-party names, emails, handles, organizations
- Verbatim phrases >5 words from any source document
- Project-specific or domain-specific acronyms

**The profile stores ONLY**:
- Aggregated stylometric statistics (sentence-length distributions, function-word
  frequencies, hedge/booster rates, etc.) — these are aggregate numbers,
  not content
- Paraphrased descriptors of writing patterns ("Names the tradeoff before
  the recommendation") — written by an LLM with explicit anti-leak rules
- Fully synthetic exemplars demonstrating each pattern with placeholder
  content (X, Y, Alice, Bob, "the team", "tool A") — never copied from source
- SHA-256 hashes of source documents (in `sources_seen.yaml`) to dedup
  re-analysis attempts

**The source index DOES store** titles + URLs because it's the user's
discovery layer (lives in their personal Drive, never in any tracked repo).

## Audience and doc-type model

Two orthogonal axes:

**Audience** (7 tags, fixed enum) — *who* the writing is for:
- `technical_peer` — Design docs, RFCs, technical messages to engineers
- `leadership` — Status updates, project pitches, escalations
- `direct_report` — 1:1 prep, feedback, coaching
- `cross_functional` — Specs, primers for PM/design partners
- `external_public` — Blog posts, talk abstracts, public PR/FAQ docs
- `casual` — DMs, casual Slack
- `self_notes` — Journals, scratchpads, unfiltered private voice

**Doc type** (3 tags, fixed enum) — *how polished* the artifact is:
- `polished` — Complete prose intended as a finished artifact
- `draft` — In-progress prose with placeholders or rough edges
- `outline` — Bullets and short fragments, structure-first not prose-first

Each combination gets its own sub-bucket inside the profile:
`audiences.<audience>.types.<doc_type>.{stats, descriptors, exemplars}`.
A writer's leadership-polished voice can differ sharply from their
leadership-outline voice — both are real voices and both matter.

**Continuous axes overlay** (Option C from the original design): each
audience also carries an `axis_baseline` over four 0-1 axes:
`formality`, `technical_density`, `brevity`, `warmth`. Write-time prompts
can nudge the baseline ("write this leadership doc but bump formality 0.1").

## Three LLM calls per document

1. **Classify** (Sonnet, ~$0.005/doc) — audience tag + doc_type + axis
   estimates + reasoning. Used for metadata-only triage on the discovery
   queue (cheap) AND for the full-content classification at propose time
   (more accurate, recorded separately from the metadata pass for evaluation).

2. **Describe** (Sonnet, ~$0.01/doc) — 10 descriptor fields under strict
   anti-leak rules. Includes voice_summary, rhetorical_moves, tics,
   structural_habits, openings/closings inventory, transition_style,
   humor_register, self_reference_behavior, what_to_avoid.

3. **Exemplify** (Sonnet, ~$0.01/doc) — for each pattern, generate one
   synthetic example with fully placeholder content. The example must
   demonstrate the pattern's structure and rhythm without containing any
   source content.

Total: ~$0.025/doc + spaCy + Brysbaert lookups (free). 20-50 docs of
corpus → ~$1 total spend.

## The scrub gate

Every LLM output passes through a deterministic scrub validator before
hitting the proposal file:

- Reject any descriptor or exemplar containing >5-word substring match with
  source
- Reject any proper-noun token not on the allowlist (months, common
  English transition words, placeholder words like "Tool", "System")
- Reject emails, URLs, @-handles, hex hashes, long digit runs (5+)
- Sentence-initial capitals (after `.!?`, newlines, bullets, list markers,
  colons) are NOT flagged

Two policies:
- **Descriptor leaks halt the proposal**. The descriptor block goes
  directly into the profile when accepted, so any flagged descriptor would
  embed source content. Retry with prompt-tightening.
- **Exemplar leaks flag the candidate but don't halt the run**. Each
  exemplar is reviewed individually anyway; flagged ones default to reject
  but the user can choose to accept-with-override.

## Profile merge math

When `/voice-review` accepts a proposal:

**Stats** are merged via weighted average:
```
new = (min(sources_count, cap) * old + k * fresh) / (min(sources_count, cap) + k)
```
where `k=1` per source and `cap=30` (so a 50-document profile still gives
new sources ~3% influence — enough to evolve, not so much that one outlier
swings the profile).

**Descriptors** — list fields (rhetorical_moves, tics, etc.) append + dedup
case-insensitively. Prose fields (voice_summary, transition_style) replace
wholesale on accept (latest wins).

**Exemplars** — append-only. Per-item accept; flagged exemplars default to
reject.

**Audience baseline axes** are weighted-averaged at the audience level
(across all doc_types).

## Schema versioning

Every persisted file declares `schema_version`. Four kinds today:
`profile_v1`, `sources_seen_v1`, `proposal_v1`, `source_index_v1`. All
use `additionalProperties: false` everywhere — adding a new field requires
a schema bump + a migration file, which means accidental field drift
fails loudly rather than silently.

Migrations live in `migrations/<kind>_v<N>_to_v<N+1>.py` and are auto-discovered
by filename. The schema runner walks the chain on load and emits clear
errors when:
- Code is older than the data ("Update the voice-analyze skill")
- A required migration file is missing in the chain

## Storage layout

```
~/.config/ai-seal-tools/voice/
  profile.yaml             ← live profile (Drive-synced)
  sources_seen.yaml        ← hash-keyed source log (Drive-synced)
  source_index.yaml        ← discovery queue with titles/URLs (Drive-synced)
  proposals/               ← pending proposals (machine-local — see Note 1)
    prop_<ts>_<hash>.yaml
  archive/                 ← accepted/rejected proposal log (Drive-synced)

~/.cache/ai-seal-tools/voice/
  content/                 ← cached document content for propose-batch
    <source_id>.md
  brysbaert_concreteness.tsv   ← optional concreteness norms
  dale_chall_easy.txt          ← optional simple-word list
```

**Note 1**: Proposals are deliberately NOT Drive-synced. They're
pre-review ephemeral state; syncing would invite "machine A generated
proposal X, machine B accepted half of it, the two diverge" foot-guns.

## Privacy guarantees (the hard rules)

1. No source document text, title, file path, or URL is ever written to
   `profile.yaml`, `sources_seen.yaml`, `proposals/`, or `archive/`.
2. No third-party name, email, handle, or organization is written to any
   of those files.
3. Exemplars are fully synthetic placeholder text, never copied or
   lightly-modified from source.
4. Every exemplar requires explicit human review before entering the
   profile.
5. The scrub validator runs on every LLM output before write; descriptor
   failures halt the run loudly, exemplar failures get flagged for review.

The source index DOES store titles + URLs for the user's own discovery —
but only locally and in their personal Drive, never in any tracked repo
or shared store.
