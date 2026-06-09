# Voice Review — Process

The review step is the **mandatory human gate** between the analyzer's
proposals and the live voice profile. Nothing enters `profile.yaml`
until the user explicitly accepts it here.

## Why a manual gate

The proposal pipeline runs LLMs over real documents. Even with the scrub
validator catching the obvious leaks (verbatim n-grams, proper nouns,
emails/URLs/hashes), some judgment calls can only be made by the person
whose voice is being modeled:

1. **Is this descriptor actually true of my voice?** — The LLM may
   confidently describe a "pattern" that's an artifact of one doc, not a
   recurring habit. Only the writer can confirm.
2. **Is this exemplar safe to commit?** — Synthetic exemplars are the
   highest-leak risk content because the LLM was told to mimic structure
   while inventing content. A scrub-passed exemplar can still feel
   suspicious in a way only the writer can judge.
3. **Should this doc count as `audience=X`?** — The classifier's
   judgment may be reasonable but wrong for this particular doc; the user
   may want to override the routing.

The reviewer enforces this gate without making it tedious — bulk-accept is
allowed for stats and descriptors, but exemplars always prompt per-item.

## Three layers of accept

| Layer | Accept granularity | Why |
|---|---|---|
| Stats (numeric block) | All-or-nothing per proposal | Numeric merges are deterministic; reviewing each metric individually adds no signal |
| Descriptors (10 prose fields) | Per-field if requested | Some fields capture the voice well; others might be junk. Per-field accept handles this. |
| Exemplars (synthetic examples) | **Per-item, mandatory** | Each one is content that goes verbatim into the profile. Each gets its own decision. |

## Merge math (what actually happens on accept)

### Stats
Weighted average against the current bucket:
```
new = (min(sources_count, cap) * old + k * fresh) / (min(sources_count, cap) + k)
```
- `k=1` per source
- `cap=30` to prevent ossification — a 50-doc profile still lets new
  sources contribute ~3% to the running mean
- First source for a bucket: takes the fresh value verbatim (no average)

### Descriptors
- **List fields** (rhetorical_moves, tics, structural_habits,
  openings_inventory, closings_inventory, what_to_avoid) — append + dedup
  case-insensitively. Doesn't grow forever because the LLM tends to
  re-state similar patterns across docs; the dedup filters those.
- **Prose fields** (voice_summary, transition_style, humor_register,
  self_reference_behavior) — replace wholesale on accept. Latest accepted
  wins.

### Exemplars
- Append-only with a strict no-overwrite rule by ID.
- Each carries `source_hash`, `reviewed_at`, optional `reviewer_notes`.
- Flagged exemplars (`scrub_status: flagged`) default to **reject** at the
  prompt; the user must explicitly choose accept-with-override, which gets
  audited via `reviewer_notes: "accepted despite scrub flag (manual override)"`.

### Audience axis baseline
Each audience's `axis_baseline` (formality, technical_density, brevity,
warmth) is updated via weighted average across all doc_types within that
audience (not per doc_type). Reflects the audience-level register, not
the polished-vs-outline split.

### Sources seen
A new entry is appended to `sources_seen.yaml` with:
- `hash` — SHA-256 of normalized source text
- `analyzed_at` — timestamp
- `audience`, `doc_type` — what bucket the doc contributed to
- `word_count` — non-identifying
- `axis_estimates` — captured for evaluation
- `contributed_exemplar_ids` — link to which exemplars came from this source

Dedup is by hash — re-applying the same proposal is a no-op.

## Lifecycle

```
~/.config/ai-seal-tools/voice/proposals/prop_<ts>_<hash>.yaml
                              │
                              ▼  /voice-review walk
                              │
            ┌─────────────────┼─────────────────┐
            │                 │                 │
        accepted            skipped         rejected
            │                 │                 │
            ▼                 ▼                 ▼
        archive/          (stays in        archive/
        prop_<id>.yaml    proposals/        (with status)
        (review_status:   pending)
         accepted)
```

`source_index.yaml` entries are updated by `propose-batch` (status:
queued → analyzed; source_hash + proposal_id linked). The reviewer doesn't
touch the index — it consumes proposals and writes profile/sources_seen.

## Two workflows

There are two ways to walk through proposals — pick whichever feels right
for the batch size:

### File-based (recommended for batches of ≥5 proposals)

Generates a single markdown file with every exemplar laid out. You edit
`decision:` lines + add `reason:` for any overrides, then apply atomically.
Much faster than the interactive prompt for large batches.

```bash
# Generate the review file from current pending proposals
UV_NO_CONFIG=1 uv run skills/voice-review/regen_exemplar_review.py

# Edit it: change `decision:` for any exemplar; add `reason:` for overrides
$EDITOR scratch/voice-corpus/exemplar_review.md

# Preview what would happen
UV_NO_CONFIG=1 uv run skills/voice-review/apply_exemplar_review.py --dry-run

# Apply: merges into profile, logs feedback, archives proposals
UV_NO_CONFIG=1 uv run skills/voice-review/apply_exemplar_review.py
```

Sibling utilities (in `skills/voice-analyze/`):
- `rescrub_proposals.py` — after updating `scrub.py` rules, re-evaluate
  flag statuses on pending proposals without re-running propose-batch
- `summarize_feedback.py` — analyze `scrub_feedback.yaml` (accumulated
  user override reasons) to find rule-tuning candidates

#### Fast-path: auto-categorize before hand review

When most of the rejects in a batch are scrub false positives (common
nouns in section labels), running `auto_categorize.py` over the review
file flips them to `accept` with sensible default reasons, so hand
review only touches the genuinely ambiguous cases.

```bash
# After regen_exemplar_review.py, before opening $EDITOR:
UV_NO_CONFIG=1 uv run skills/voice-review/auto_categorize.py
```

Heuristic per flagged reject:
- Any proper-noun flag adjacent to a placeholder first name OR after
  an explicit people-context marker (`Attendees:`, `By:`, `@`) →
  `accept` + `substitute: auto` (substituter rewrites on merge).
- All flags are common nouns in section/title positions →
  `accept` + `reason: false_positive:section_label`.
- Anything else (ngram-overlap flags etc.) → left as `reject` for
  reviewer judgment.

#### Fixing mistakes after merge

`profile_edit.py` makes targeted corrections to the live profile when
the normal review-and-merge path produced something you want to retract
or add back. Every operation appends to `merge_history` so the audit
trail stays intact.

```bash
# Remove already-merged exemplars by id
UV_NO_CONFIG=1 uv run skills/voice-review/profile_edit.py remove ex_xxx_001 ex_xxx_002

# Restore an exemplar that was previously rejected (pulls from archive)
UV_NO_CONFIG=1 uv run skills/voice-review/profile_edit.py restore ex_yyy_003 \
    --reason true_positive:override --notes "useful pattern, accepting the leak"

# Restore with auto-substitution (placeholder names, <N> for multi-digit numbers)
UV_NO_CONFIG=1 uv run skills/voice-review/profile_edit.py restore ex_zzz_007 \
    --reason false_positive:common_word --substitute

# Replace reviewer_notes on an exemplar that's already in the profile
UV_NO_CONFIG=1 uv run skills/voice-review/profile_edit.py update-notes ex_aaa_004 \
    --reason false_positive:emphasis --notes "bold styling, not a name"
```

### Interactive (single proposals, careful reviews)

Prompts per-piece. Better for inspecting one proposal in detail.

```bash
# Show what's pending without prompting
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py list

# Walk all pending proposals in order (oldest first)
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk

# Walk a specific proposal
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk \
    --proposal ~/.config/ai-seal-tools/voice/proposals/prop_<id>.yaml

# Auto-accept stats and descriptors (exemplars still prompt per-item)
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk --yes

# See what would change without writing
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk --dry-run
```

`--yes` only relaxes the stats and descriptor gates. Exemplars always
prompt — that's the hard rule, no flag to override it.

## Per-proposal prompt flow

For each proposal the reviewer:

1. **Shows the classification** (audience + doc_type), confidence, and
   any user override. Prompts to confirm or skip.
2. **Stats**: shows the stats block summary. Prompts `[yes/no]` to merge.
3. **Descriptors**: shows the 10-field block. Prompts:
   - `yes` — accept all fields
   - `no` — skip the descriptor merge entirely
   - `per-field` — walk each field with accept/reject prompts
4. **Exemplars** (always interactive): for each candidate:
   - Shows pattern, synthetic example, when_to_use
   - Shows scrub findings inline if `scrub_status: flagged`
   - Default action: `reject` if flagged, `accept` if passed
   - User picks `accept / reject / skip`
   - Accepted-with-override (on flagged) gets logged in reviewer_notes
5. **Updates profile**: merge math runs, `sources_seen.yaml` gains a
   record, `merge_history` gets an entry, proposal moves to archive.

## What happens to bad proposals

If you decide a proposal is just wrong (audience mis-classified beyond
override, exemplars all flagged, descriptors not capturing your voice):

- Mark `review_status: rejected` in the proposal file (manual edit)
- Move it to `archive/` (it won't be picked up again)
- The source can be re-proposed later from a refreshed prompt by setting
  the source-index entry's `status` back to `queued`

## Iterating on prompts

The proposal generation can be re-run on any document. If a batch of
descriptors comes out shallow or leaky, tighten the system prompts in
`skills/voice-analyze/llm.py` and re-run:

```bash
# Re-set status to queued for the source you want to retry
# (manual yaml edit in ~/.config/ai-seal-tools/voice/source_index.yaml)

UV_NO_CONFIG=1 uv run skills/voice-analyze/analyzer.py propose-batch
```

Each propose run produces a fresh proposal id, so the old one (if
archived) stays for comparison.
