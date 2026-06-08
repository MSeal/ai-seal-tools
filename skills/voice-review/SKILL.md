---
name: voice-review
description: Walk pending voice-analysis proposals interactively → merge accepted pieces into the live voice profile. This is the mandatory human gate where LLM-generated EXEMPLARS (synthetic placeholder-content patterns) and PARAPHRASED DESCRIPTORS become permanent profile content. Exemplars require explicit per-item approval — no bulk-accept flag exists for them, because each one is content stored verbatim in the profile and carries the highest leak risk. Stats merge via weighted average across sources (capped at 30 to allow continued evolution). Descriptor list-fields dedup-append, prose fields replace wholesale on accept. Source documents themselves (the seeds) are never read here — only the proposal yamls produced by /voice-analyze, which contain no source text. Pending proposals are machine-local at ~/.config/ai-seal-tools/voice/proposals/; accepted ones move to the Drive-synced archive/ and update the personal-Drive profile.yaml + sources_seen.yaml. Argument is optional path to a specific proposal yaml.
---

# Voice Review

`/voice-review [proposal-path]` walks one or more proposal YAML files from
`~/.config/ai-seal-tools/voice/proposals/` and lets the user accept or reject
each piece before it's merged into the live profile.

## Flow

For each proposal:

1. **Show the classification** (audience + doc_type) and how it was decided
   (classifier confidence vs manual override). Confirm or skip.
2. **Stats**: show before/after summary of how each metric would shift under
   the weighted merge. Prompt `[accept all / skip stats]`. Stats are merged
   via `new = (sources_count * old + k * fresh) / (sources_count + k)` with
   `k=1` per source and `sources_count` capped at 30.
3. **Descriptors**: show each new descriptor field side-by-side with the
   current profile value, prompt `[accept / reject / edit]`. Multi-item
   fields (rhetorical_moves, tics, etc.) are merged via LLM synthesis after
   review.
4. **Exemplars** (mandatory per-item review): each exemplar displayed in
   full. Prompt `[accept / reject / edit / skip]`. Flagged exemplars
   (scrub_status: flagged) are shown with their findings inline and
   default-suggest reject.
5. **Anti-patterns**: if the proposal stats reveal a clear anti-pattern
   (e.g. em-dash rate WAY below Claude's baseline), update the derived
   anti-pattern threshold.

On completion:
- Update `profile.yaml` with accepted changes
- Append a `merge_history` entry recording the proposal id, doc count, and
  notes
- Move the proposal file to `~/.config/ai-seal-tools/voice/archive/`
- Update `sources_seen.yaml` with the source_hash + analyzed_at + audience

## Invocation

```bash
# Review every pending proposal in order (oldest first):
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk

# Review a specific proposal:
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py walk \
    --proposal ~/.config/ai-seal-tools/voice/proposals/prop_2026...yaml

# Print summary of pending proposals without prompting:
UV_NO_CONFIG=1 uv run skills/voice-review/reviewer.py list
```

## What gets persisted

| File | Updated |
|---|---|
| `~/.config/ai-seal-tools/voice/profile.yaml` | Stats merged, descriptors merged, accepted exemplars appended |
| `~/.config/ai-seal-tools/voice/sources_seen.yaml` | One new entry per accepted proposal (hash + audience + doc_type + analyzed_at) |
| `~/.config/ai-seal-tools/voice/source_index.yaml` | Linked entry's status stays `analyzed` (already set by propose-batch) |
| `~/.config/ai-seal-tools/voice/proposals/<id>.yaml` | Moved to `archive/` |

## Anti-leak gate

Exemplars MUST be approved per-item. The reviewer:
- Refuses to merge exemplars with `scrub_status: flagged` unless the user
  explicitly chooses `accept-despite-flag` (which is itself audited in
  reviewer_notes)
- Re-runs the scrub validator on any edits the user makes
- Never auto-accepts exemplars even with `--yes-to-all` style flags

This is a hard rule: exemplars are the highest-leak-risk content in the
profile, so they get the strictest gate.
