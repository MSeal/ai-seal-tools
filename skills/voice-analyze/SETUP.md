# voice-analyze setup

## Required: Anthropic API access

The analyzer's `classify` and `propose` sub-commands call Claude. The client
is selected automatically:

- **Vertex AI** (when `CLAUDE_CODE_USE_VERTEX` is set): uses
  `anthropic.AnthropicVertex()`, which reads project/region from
  `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` (or
  `GOOGLE_CLOUD_REGION`). Credentials come from `gcloud auth
  application-default login` — refresh if you hit a `RefreshError`.
- **Standard API**: uses `anthropic.Anthropic()`, which reads
  `ANTHROPIC_API_KEY`.

If the default model name (`claude-sonnet-4-6`) needs adjustment for your
Vertex region (e.g. `claude-sonnet-4-6@<date>`), pass `--model <id>` to the
analyzer (TODO — not yet wired) or edit `DEFAULT_MODEL` in `llm.py`.

## Required: spaCy English model

The deterministic stats extractor needs spaCy's small English model for
sentence segmentation, POS tagging, and passive-voice detection. Install it
once:

```bash
UV_NO_CONFIG=1 uv run python -m spacy download en_core_web_sm
```

`UV_NO_CONFIG=1` routes uv through public PyPI rather than this machine's
Confluent CodeArtifact registry (see the repo's `.envrc` for the auto-config).

## Optional: Brysbaert concreteness norms

The concreteness metric (in `stats.lexical_complexity.concreteness_score`)
requires the Brysbaert, Warriner & Kuperman (2014) concreteness ratings
lexicon — a ~40k-word file (~2MB) released as supplementary material to:

> Brysbaert, M., Warriner, A. B., & Kuperman, V. (2014). Concreteness
> ratings for 40 thousand generally known English word lemmas. Behavior
> Research Methods, 46, 904-911.

The lexicon is *not* checked into this repo. If installed, the extractor
loads it and emits a `concreteness_score` when the doc has ≥40% coverage
against it. If not installed, the metric is silently skipped — everything
else still works.

### Install steps

1. Download the supplementary data file (`Concreteness_ratings_Brysbaert_et_al_BRM.xlsx`)
   from <http://crr.ugent.be/archives/1330>.
2. Convert to TSV (the loader expects tab-separated). One easy way:

   ```python
   import openpyxl, csv
   wb = openpyxl.load_workbook("Concreteness_ratings_Brysbaert_et_al_BRM.xlsx")
   ws = wb.active
   with open("brysbaert_concreteness.tsv", "w") as out:
       w = csv.writer(out, delimiter="\t")
       for row in ws.iter_rows(values_only=True):
           w.writerow(row)
   ```

3. Move the resulting TSV to the cache directory:

   ```bash
   mkdir -p ~/.cache/ai-seal-tools/voice
   mv brysbaert_concreteness.tsv ~/.cache/ai-seal-tools/voice/
   ```

## Optional: Dale-Chall easy-word list

The `dale_chall_simple_pct` metric requires a Dale-Chall (or equivalent
simple-word) reference list. Format: one lowercase word per line, blank
lines and `#`-prefixed comments allowed. Save to:

```
~/.cache/ai-seal-tools/voice/dale_chall_easy.txt
```

If absent, the metric is skipped.

## Where state lives

| Path | Purpose | Required |
|---|---|---|
| `~/.config/ai-seal-tools/voice/profile.yaml` | Live voice profile (Drive-backed via `links.yaml`) | Yes |
| `~/.config/ai-seal-tools/voice/sources_seen.yaml` | Hash-keyed source log | Yes |
| `~/.cache/ai-seal-tools/voice/brysbaert_concreteness.tsv` | Concreteness lexicon | Optional |
| `~/.cache/ai-seal-tools/voice/dale_chall_easy.txt` | Simple-word list | Optional |
