# Voice Profile — Metrics

Every metric tracked by the deterministic stats extractor, what it measures,
and what it's intended to capture about voice. All metrics live under
`audiences.<audience>.types.<doc_type>.stats` in the profile and are
weighted-averaged across contributing sources.

Sources for the metric design are listed at the end (stylometry + LLM-voice
research that informed the choices).

## Sentence-shape

| Metric | What it is | Why we track it |
|---|---|---|
| `sentence_length.mean` | Mean tokens per sentence | The most basic readability signal; tracks whether a writer leans long-form or punchy |
| `sentence_length.median` | Median tokens per sentence | Robust to a single very long or very short sentence pulling the mean |
| `sentence_length.p10`, `.p90` | 10th and 90th percentile | Spread — does the writer mix short and long, or stick to a band? |
| `sentence_length.std` | Standard deviation | Same idea, single-number summary |
| `sentence_length.lag1_autocorr` | Pearson correlation between adjacent sentence lengths | Captures whether the writer alternates short/long ("rhythm") or clusters them. Distinct from std — same std can come from monotone OR punchy-then-long. |
| `paragraph_length_sentences.{mean, median, p10, p90, std, lag1_autocorr}` | Same shape, sentences per paragraph | Macro-rhythm — short paragraphs vs dense prose |

## Function words (stylometry workhorse)

| Metric | What it is |
|---|---|
| `function_words_per_100w.<word>` | Per-100-word frequency of each closed-class function word (the, of, and, to, a, in, that, etc. — ~50 tracked) |

Function-word frequencies are the **most-studied stylometric signal** in
authorship attribution. They're topic-independent (a doc about Kafka and
a doc about cooking have similar `the` rates if the same person wrote
them) and stable per-author over time. Used in tools from Burrows' Delta
(1990s) to modern LLM-vs-human detectors.

## Pronoun ratios

| Metric | What it is |
|---|---|
| `pronoun_ratios.i_me` | I/me/my/mine/myself as fraction of all words |
| `pronoun_ratios.we_us` | We/us/our/ours/ourselves |
| `pronoun_ratios.you_your` | You/your/yours/yourself/yourselves |
| `pronoun_ratios.they_them` | They/them/their/theirs/themselves |
| `pronoun_ratios.third_singular` | He/she/him/her/it + possessives + reflexives |

The I-vs-we-vs-you balance is voice-defining and audience-sensitive.
Engineers writing for peers often skew "we"; engineers writing externally
often skew "you" (reader-engaging); engineers writing self-notes often
skew "I" (or absent pronouns entirely).

## Stance and engagement

| Metric | Counts |
|---|---|
| `hedge_per_100w` | "maybe", "perhaps", "probably", "somewhat", "likely", "seems", "appears", "tend to", etc. |
| `booster_per_100w` | "clearly", "obviously", "definitely", "must", "essentially", "fundamentally", etc. |
| `stance_per_100w` | "unfortunately", "remarkably", "frankly", "honestly", "importantly", etc. |
| `engagement_per_100w` | "consider", "imagine", "note", "recall", "remember", "notice" (direct reader address verbs) |

Hedges soften assertions; boosters strengthen them. The ratio between the
two is a strong stylistic marker — academic prose typically high-hedge,
sales copy typically high-booster. Stance markers signal author attitude.
Engagement markers signal reader orientation.

## Punctuation per 100 words

| Metric | What it counts |
|---|---|
| `punctuation_per_100w.em_dash` | — (em-dash) |
| `punctuation_per_100w.en_dash` | – (en-dash) |
| `punctuation_per_100w.hyphen` | - (hyphen) |
| `punctuation_per_100w.parenthesis` | ( count (proxy for paren pairs) |
| `punctuation_per_100w.semicolon` | ; |
| `punctuation_per_100w.colon` | : |
| `punctuation_per_100w.exclamation` | ! |
| `punctuation_per_100w.question` | ? |
| `punctuation_per_100w.ellipsis` | … |

**Em-dash is the most-watched punctuation signal**: Claude defaults to
~3 em-dashes per 100 words, while typical human writing is ~0-1.
A derived anti-pattern (`ap_em_dash_overuse`) is calibrated against the
profile's actual em-dash rate so write-time prompts can pull toward the
writer's real baseline.

## Syntactic features (POS-dependent, requires spaCy)

| Metric | What it is |
|---|---|
| `passive_voice_rate` | Fraction of sentences with at least one `nsubjpass` or `auxpass` dependency |
| `sentence_opener_pos_dist.<category>` | Distribution over coarse opener types (subject_np, pronoun, adverbial, subordinator, conjunction, gerund, imperative, prepositional, adjective, interjection) |

Sentence-opener distribution is the **second-most diagnostic qualitative
marker after function-word frequencies** (per the stylometry literature).
Writers have strong unconscious habits — some are subject-NP-heavy, some
open with conjunctions ("And...", "But..."), some lean on gerunds
("Considering the...").

## Connective preferences

| Metric | What it is |
|---|---|
| `connective_prefs.contrast.<word>` | Given a contrast connective was used, fraction that were each word (but, however, though, yet, still, etc.) |
| `connective_prefs.additive.<word>` | Same for additive (and, also, furthermore, etc.) |
| `connective_prefs.causal.<word>` | Same for causal (because, since, so, therefore, etc.) |
| `connective_prefs.temporal.<word>` | Same for temporal (then, next, after, before, etc.) |

Captures choice patterns within categories. Two writers might use
contrast connectives at the same rate, but one favors "but" while the
other favors "however" — that's voice.

## Contraction rate

| Metric | What it is |
|---|---|
| `contraction_rate` | Fraction of words that are contractions (n't, 're, 've, 'll, etc.) |

Casual writing → high contraction rate. Formal writing → low. Strong
audience signal.

## Bullet density

| Metric | What it is |
|---|---|
| `bullet_density` | Fraction of non-blank lines that look like bullets (- * + • or numbered) |

Discriminates `outline` from `polished` doc_types sharply (observed in
seed: 0.87 outline vs 0.00 polished).

## Lexical diversity

| Metric | What it is |
|---|---|
| `lexical_diversity.mtld` | Measure of Textual Lexical Diversity — McCarthy & Jarvis (2010). Length-stable. |
| `lexical_diversity.moving_ttr` | Moving-window type-token ratio (window=50) |

Captures vocabulary range. Raw TTR (types ÷ tokens) drops with document
length, so we use the length-stable variants.

## Lexical complexity

| Metric | What it is |
|---|---|
| `lexical_complexity.avg_word_length_chars` | Mean characters per word |
| `lexical_complexity.avg_word_syllables` | Mean syllables (heuristic vowel-group count, ~85% accurate) |
| `lexical_complexity.pct_long_words` | Fraction of words ≥7 characters (Lix readability core) |
| `lexical_complexity.dale_chall_simple_pct` | Fraction of words in the Dale-Chall easy-word list (~3000 everyday words). Optional — requires lexicon. |
| `lexical_complexity.latinate_ratio` | Fraction of words ending in -tion, -ity, -ate, -ize, -ence, -ous, -ive, -ment, etc. Used as a register proxy ("utilize" vs "use"). |
| `lexical_complexity.concreteness_score` | Mean Brysbaert concreteness rating (1.0=fully abstract, 5.0=fully concrete). Optional — requires lexicon. Skipped if doc has <40% coverage in the lexicon. |

These five-plus dimensions capture different facets of "simple vs complex
word choice":
- Length and syllables → mechanical complexity
- Long-word % → quick proxy
- Dale-Chall → standard "is this plain English?" benchmark
- Latinate ratio → register choice ("utilize" vs "use")
- **Concreteness** → orthogonal axis (concrete vs abstract, not simple vs
  complex). The only metric in the schema that captures the "ship faster"
  (concrete) vs "improve velocity" (abstract) distinction.

The Latinate suffix heuristic was specifically chosen over a Dale-Chall-
based estimate to avoid inflating the metric with technical jargon
("Kafka", "broker", "OAuth" aren't in Dale-Chall but also aren't
Latinate — they shouldn't count toward register complexity).

## Readability

| Metric | What it is |
|---|---|
| `readability.fk_grade` | Flesch-Kincaid grade level: `0.39 * (words/sentences) + 11.8 * (syllables/words) - 15.59` |
| `readability.coleman_liau` | Coleman-Liau index: `0.0588 * L - 0.296 * S - 15.8` where L = chars per 100 words, S = sentences per 100 words |
| `readability.gunning_fog` | Gunning Fog: `0.4 * ((words/sentences) + 100 * (complex_words / words))` where complex_words = 3+ syllables |

Three different formulae for the same target — they disagree on which
features matter, so we track all three. Useful to spot when a doc is an
outlier on one but not the others.

## Descriptors (qualitative, LLM-extracted)

These don't have numeric values — they're paraphrased prose descriptions.
Stored under `audiences.<audience>.types.<doc_type>.descriptors`:

| Field | What it captures |
|---|---|
| `voice_summary` | 2-3 sentences: tone, register, energy |
| `rhetorical_moves[]` | Structural argument moves ("Names the tradeoff before the recommendation") |
| `tics[]` | Small habits (soft-recommendation markers, sentence-shape preferences) |
| `structural_habits[]` | Macro-structure patterns (opening → narrowing → risks) |
| `openings_inventory[]` | How the writer starts documents |
| `closings_inventory[]` | How the writer ends documents |
| `transition_style` | How they pivot between sections (prose) |
| `humor_register` | Where and how humor appears, or "none observed" |
| `self_reference_behavior` | How they refer to themselves (I, we, none) |
| `what_to_avoid[]` | Patterns that would feel UN-LIKE the writer (e.g. "avoid em-dashes for asides") |

## Anti-patterns (shared, audience-independent)

Stored under `shared_anti_patterns[]` — voice-independent rules that fight
against AI-default writing patterns:

| ID | Rule | Derived from |
|---|---|---|
| `ap_bold_term_bullets` | Never use **bold term:** explanation bullets | Constant (the single most identifiable AI tell) |
| `ap_filler_phrases` | Avoid: "It's worth noting", "delve into", "dive into", "pivotal", "seamless", "robust", "utilize" | Constant |
| `ap_performative_enthusiasm` | Avoid: "Great question", "I'm excited to", "incredible", "powerful" | Constant |
| `ap_signposting` | Avoid signposting: "Let's explore", "Now let's turn to" | Constant |
| `ap_balanced_bothsidesism` | Avoid "On the one hand X, on the other Y, ultimately Z" closers | Constant |
| `ap_em_dash_overuse` | Em-dash rate must stay near baseline | **Derived** from `punctuation_per_100w.em_dash` |

## Cost per document

Per `propose` call:
- 1 spaCy parse (~1-2s)
- 3 Claude Sonnet calls (~$0.025 total)
- Scrub validator (deterministic, <100ms)

Per `index classify` call (metadata-only):
- 1 Claude Sonnet call (~$0.005)

A typical batch of 20 docs costs about $0.50.

## Evaluation (planned)

Once the corpus is established, we'll add:
- **Audience prediction accuracy**: re-classify each doc from full content
  and compare against manual audience labels. Tracks classifier drift.
- **Profile stability**: re-merge the same N documents in different orders
  and check that final profile is the same modulo merge order
- **Generation fidelity**: blind A/B test of `voice-write` outputs against
  real source-doc snippets (with the writer rating them)

## References

The metric design draws on:

- **Function words as authorship signal** — Burrows, J. F. (2002).
  *Computers and the Humanities, 36*. The Delta-paper that established
  function-word frequencies as the stylometry workhorse.
- **MTLD** — McCarthy, P. M. & Jarvis, S. (2010). MTLD, vocd-D, and HD-D:
  A validation study. *Behavior Research Methods, 42*.
- **Brysbaert concreteness norms** — Brysbaert, M., Warriner, A. B., &
  Kuperman, V. (2014). Concreteness ratings for 40 thousand generally
  known English word lemmas. *Behavior Research Methods, 46*.
- **Authorial voice components** (rhetorical moves, hedges, boosters,
  stance, engagement) — Hyland's stance-and-engagement framework, widely
  used in academic-discourse analysis.
- **Claude default-voice anti-patterns** — observed across the AI-detection
  and "stop AI-sounding writing" literature: em-dash overuse, bold-term
  bullets, filler phrases, performative enthusiasm, signposting.
