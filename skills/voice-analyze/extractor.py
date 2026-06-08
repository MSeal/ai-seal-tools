"""Deterministic stylometric stats extraction for a document.

Returns a dict matching the `stats` block of the v1 profile schema. No LLM
calls — pure Python. The qualitative side (descriptors, exemplars) is
computed separately by analyzer.py via Claude.

The output dict is intended to be merged into
`profile.audiences.<tag>.types.<doc_type>.stats` after weighted averaging
across sources. The extractor itself does not know about audience or
doc_type — that classification is done upstream.

POS-dependent stats (sentence_opener_pos_dist, passive_voice_rate) require
spaCy's `en_core_web_sm` model. Lexicon-dependent stats
(concreteness_score, dale_chall_simple_pct) are skipped if the lexicons
aren't installed. Everything else is unconditional.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from typing import Any

import spacy
from spacy.tokens import Doc, Token

from lexicons import (
    BOOSTERS,
    CONNECTIVES,
    ENGAGEMENT,
    FUNCTION_WORDS,
    HEDGES,
    LATINATE_SUFFIXES,
    PRONOUN_GROUPS,
    STANCE,
    TRACKED_PUNCTUATION,
    Lexicons,
    stem_for_lookup,
)


# ---------- Normalization & sentence/paragraph splitting -----------------------

def normalize_text(text: str) -> str:
    """NFC-normalize and collapse internal whitespace (preserve paragraph breaks)."""
    nfc = unicodedata.normalize("NFC", text)
    # Collapse runs of spaces/tabs but keep newlines so paragraph splitting works
    nfc = re.sub(r"[ \t]+", " ", nfc)
    # Collapse runs of 3+ newlines down to exactly 2 (paragraph separator)
    nfc = re.sub(r"\n{3,}", "\n\n", nfc)
    return nfc.strip()


def split_paragraphs(text: str) -> list[str]:
    """Paragraphs are separated by blank lines."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return parts


_BULLET_LINE_RE = re.compile(r"^\s*([-*+•]|\d+[.)])\s+")


def is_bullet_line(line: str) -> bool:
    return bool(_BULLET_LINE_RE.match(line))


def bullet_density(text: str) -> float:
    """Fraction of non-blank lines that look like bullets. Discriminates
    outline-mode writing from prose."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    bullets = sum(1 for ln in lines if is_bullet_line(ln))
    return bullets / len(lines)


# ---------- Token helpers ------------------------------------------------------

def content_tokens(doc: Doc) -> list[Token]:
    """Tokens that count as 'words' — excludes punct, spaces, line breaks."""
    return [t for t in doc if not t.is_space and not t.is_punct]


def lowercase_word_tokens(doc: Doc) -> list[str]:
    return [t.text.lower() for t in content_tokens(doc)]


# ---------- Distribution stats -------------------------------------------------

def _distribution(values: list[float]) -> dict[str, float]:
    """mean/median/p10/p90/std/lag1_autocorr — matches the schema's
    distribution_stats shape."""
    if not values:
        return {}
    sorted_v = sorted(values)
    n = len(sorted_v)
    out = {
        "mean": float(statistics.fmean(sorted_v)),
        "median": float(statistics.median(sorted_v)),
        "p10": float(_percentile(sorted_v, 10)),
        "p90": float(_percentile(sorted_v, 90)),
        "std": float(statistics.pstdev(sorted_v)) if n > 1 else 0.0,
    }
    if n >= 3:
        out["lag1_autocorr"] = _lag1_autocorr(values)
    return out


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _lag1_autocorr(values: list[float]) -> float:
    """Pearson correlation between values[:-1] and values[1:]. Captures whether
    the writer alternates short/long sentences or clusters them."""
    if len(values) < 3:
        return 0.0
    a = values[:-1]
    b = values[1:]
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a)
    db = sum((y - mb) ** 2 for y in b)
    denom = (da * db) ** 0.5
    if denom == 0:
        return 0.0
    return num / denom


# ---------- Sentence and paragraph length --------------------------------------

def sentence_length_stats(doc: Doc) -> dict[str, float]:
    lengths = [
        len([t for t in s if not t.is_space and not t.is_punct])
        for s in doc.sents
    ]
    return _distribution([float(x) for x in lengths if x > 0])


def paragraph_length_stats_sentences(doc: Doc, text: str) -> dict[str, float]:
    """Sentences per paragraph. We count sentences within each blank-line-separated
    paragraph in the original text by re-parsing each."""
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return {}
    nlp = doc.vocab  # not used here; we use spaCy sentencizer on per-paragraph subdocs
    # Cheaper: count sentence-terminator regex hits per paragraph. spaCy's sentence
    # boundaries don't directly map to paragraph boundaries without re-parsing.
    counts: list[float] = []
    for p in paragraphs:
        # Crude sentence count: number of [.!?] followed by space/end, min 1
        n = max(1, len(re.findall(r"[.!?](?=\s|$)", p)))
        counts.append(float(n))
    return _distribution(counts)


# ---------- Function words & pronouns ------------------------------------------

def function_words_per_100w(words: list[str]) -> dict[str, float]:
    if not words:
        return {}
    n = len(words)
    counter = Counter(w for w in words if w in FUNCTION_WORDS)
    return {w: counter[w] * 100.0 / n for w in counter}


def pronoun_ratios(words: list[str]) -> dict[str, float]:
    if not words:
        return {}
    n = len(words)
    out: dict[str, float] = {}
    for group, members in PRONOUN_GROUPS.items():
        c = sum(1 for w in words if w in members)
        out[group] = c / n if n else 0.0
    return out


# ---------- Punctuation --------------------------------------------------------

def punctuation_per_100w(text: str, word_count: int) -> dict[str, float]:
    if word_count == 0:
        return {}
    counts: dict[str, float] = {}
    for label, ch in TRACKED_PUNCTUATION.items():
        c = text.count(ch)
        # Hyphen needs to exclude em-dash and en-dash which contain "-" semantics
        # only in some fonts; the chars themselves are distinct so a direct
        # count is fine.
        counts[label] = c * 100.0 / word_count
    return counts


# ---------- Hedge / booster / stance / engagement ------------------------------

def _rate_per_100w(words: list[str], lexicon: frozenset[str]) -> float:
    if not words:
        return 0.0
    c = sum(1 for w in words if w in lexicon)
    return c * 100.0 / len(words)


def hedge_per_100w(words: list[str]) -> float:
    return _rate_per_100w(words, HEDGES)


def booster_per_100w(words: list[str]) -> float:
    return _rate_per_100w(words, BOOSTERS)


def stance_per_100w(words: list[str]) -> float:
    return _rate_per_100w(words, STANCE)


def engagement_per_100w(words: list[str]) -> float:
    return _rate_per_100w(words, ENGAGEMENT)


# ---------- Contractions & passive ---------------------------------------------

_CONTRACTION_RE = re.compile(r"\b\w+'\w+\b")


def contraction_rate(text: str, word_count: int) -> float:
    """Contractions per word. Captures 're, 've, 'll, n't, etc."""
    if word_count == 0:
        return 0.0
    n = len(_CONTRACTION_RE.findall(text))
    return min(n / word_count, 1.0)


def passive_voice_rate(doc: Doc) -> float:
    """Fraction of sentences with at least one passive construction.

    Uses spaCy's dependency labels: 'nsubjpass' or 'auxpass' anywhere in the
    sentence marks it as passive. This is the standard heuristic; ~90% accurate.
    """
    sents = list(doc.sents)
    if not sents:
        return 0.0
    passive = 0
    for s in sents:
        if any(t.dep_ in ("nsubjpass", "auxpass") for t in s):
            passive += 1
    return passive / len(sents)


# ---------- Lexical diversity --------------------------------------------------

def lexical_diversity(words: list[str]) -> dict[str, float]:
    """MTLD + moving-window TTR. Resistant to document length.

    MTLD = number of words divided by mean segment length, where segments are
    grown until cumulative TTR drops below 0.72. Standard formulation
    (McCarthy & Jarvis 2010).
    """
    if len(words) < 50:
        # MTLD is unstable on very short docs; fall back to plain TTR for both.
        types = len(set(words))
        ttr = types / len(words) if words else 0.0
        return {"mtld": float(types), "moving_ttr": ttr}

    moving_ttr = _moving_ttr(words, window=50)
    mtld_score = _mtld(words, threshold=0.72)
    return {"mtld": mtld_score, "moving_ttr": moving_ttr}


def _moving_ttr(words: list[str], window: int) -> float:
    if len(words) < window:
        return len(set(words)) / len(words) if words else 0.0
    ratios = []
    for i in range(len(words) - window + 1):
        chunk = words[i : i + window]
        ratios.append(len(set(chunk)) / window)
    return float(statistics.fmean(ratios))


def _mtld(words: list[str], threshold: float) -> float:
    def _factor_count(seq: list[str]) -> float:
        factors = 0
        types: set[str] = set()
        token_count = 0
        for w in seq:
            types.add(w)
            token_count += 1
            ttr = len(types) / token_count
            if ttr <= threshold:
                factors += 1
                types.clear()
                token_count = 0
        if token_count > 0:
            # Partial factor — fractional contribution
            ttr = len(types) / token_count if token_count else 1.0
            partial = (1 - ttr) / (1 - threshold) if (1 - threshold) > 0 else 0
            factors += partial
        return factors if factors > 0 else 1

    forward = len(words) / _factor_count(words)
    backward = len(words) / _factor_count(list(reversed(words)))
    return float((forward + backward) / 2)


# ---------- Lexical complexity -------------------------------------------------

_VOWEL_GROUPS_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)


def _approx_syllables(word: str) -> int:
    """Heuristic syllable counter — vowel-group count, with silent-e adjustment.
    Good enough for distributional stats; ~85% accurate vs CMU dict."""
    w = word.lower()
    if not w:
        return 0
    groups = _VOWEL_GROUPS_RE.findall(w)
    n = len(groups)
    # Silent e at end ("name", "code")
    if n > 1 and w.endswith("e") and not w.endswith("le"):
        n -= 1
    return max(1, n)


def _is_latinate_suffix(word: str) -> bool:
    w = word.lower()
    return any(w.endswith(suf) for suf in LATINATE_SUFFIXES)


def lexical_complexity(words: list[str], lexicons: Lexicons | None) -> dict[str, float]:
    if not words:
        return {}
    # Filter to alphabetic words only — exclude numbers, mixed alphanumeric IDs.
    alpha = [w for w in words if w.isalpha()]
    if not alpha:
        return {}

    avg_chars = statistics.fmean(len(w) for w in alpha)
    avg_syl = statistics.fmean(_approx_syllables(w) for w in alpha)
    pct_long = sum(1 for w in alpha if len(w) >= 7) / len(alpha)
    latinate = sum(1 for w in alpha if _is_latinate_suffix(w)) / len(alpha)

    out: dict[str, float] = {
        "avg_word_length_chars": float(avg_chars),
        "avg_word_syllables": float(avg_syl),
        "pct_long_words": float(pct_long),
        "latinate_ratio": float(latinate),
    }

    if lexicons and lexicons.dale_chall_easy is not None:
        easy = lexicons.dale_chall_easy
        simple = sum(1 for w in alpha if w in easy) / len(alpha)
        out["dale_chall_simple_pct"] = float(simple)

    if lexicons and lexicons.concreteness is not None:
        conc = lexicons.concreteness
        scores = []
        for w in alpha:
            for variant in stem_for_lookup(w):
                if variant in conc:
                    scores.append(conc[variant])
                    break
        coverage = len(scores) / len(alpha) if alpha else 0.0
        if coverage >= 0.4:
            out["concreteness_score"] = float(statistics.fmean(scores))
        # If coverage < 0.4, skip the metric — too unreliable.
        # The coverage value itself isn't in the v1 schema; we drop it.

    return out


# ---------- Readability --------------------------------------------------------

def readability(text: str, doc: Doc) -> dict[str, float]:
    """Flesch-Kincaid grade level, Coleman-Liau, Gunning Fog."""
    words = [t.text for t in content_tokens(doc) if any(c.isalpha() for c in t.text)]
    if not words:
        return {}
    n_words = len(words)
    n_sentences = max(1, sum(1 for _ in doc.sents))
    syllables = sum(_approx_syllables(w) for w in words)
    chars = sum(len(w) for w in words)

    # Flesch-Kincaid grade level
    fk = 0.39 * (n_words / n_sentences) + 11.8 * (syllables / n_words) - 15.59

    # Coleman-Liau index
    l = 100 * chars / n_words  # average # of chars per 100 words
    s = 100 * n_sentences / n_words
    cli = 0.0588 * l - 0.296 * s - 15.8

    # Gunning Fog — uses count of complex (3+ syllable) words
    complex_words = sum(1 for w in words if _approx_syllables(w) >= 3)
    fog = 0.4 * ((n_words / n_sentences) + 100 * complex_words / n_words)

    return {
        "fk_grade": float(fk),
        "coleman_liau": float(cli),
        "gunning_fog": float(fog),
    }


# ---------- Sentence opener POS distribution -----------------------------------

# Map spaCy POS tags to our coarser categories. The categories are chosen to
# capture stylistically meaningful opener types — subject NPs feel formal,
# adverbial openers and conjunctions feel conversational, gerunds feel
# academic, etc.
_OPENER_CATEGORIES: dict[str, str] = {
    "NOUN": "subject_np",
    "PROPN": "subject_np",
    "PRON": "pronoun",
    "DET": "subject_np",        # "The team..." — DET counts as subject NP
    "ADV": "adverbial",
    "VERB": "imperative_or_gerund",   # ambiguous; refined below
    "AUX": "imperative_or_gerund",
    "SCONJ": "subordinator",    # "Because...", "While..."
    "CCONJ": "conjunction",     # "And...", "But..."
    "ADJ": "adjective",
    "ADP": "prepositional",     # "In the..."
    "INTJ": "interjection",
}


def sentence_opener_pos_dist(doc: Doc) -> dict[str, float]:
    """Distribution over opener categories. Counts sentences by the first
    content token's coarse POS."""
    sents = list(doc.sents)
    if not sents:
        return {}
    counter: Counter[str] = Counter()
    total = 0
    for s in sents:
        first = next(
            (t for t in s if not t.is_space and not t.is_punct),
            None,
        )
        if first is None:
            continue
        # Refine VERB/AUX opener: if the verb tag is VBG (gerund-ish), call it gerund
        if first.pos_ in ("VERB", "AUX"):
            if first.tag_ == "VBG":
                category = "gerund"
            else:
                category = "imperative"
        else:
            category = _OPENER_CATEGORIES.get(first.pos_, "other")
        counter[category] += 1
        total += 1
    return {cat: counter[cat] / total for cat in counter}


# ---------- Connective preference ----------------------------------------------

def connective_prefs(words: list[str]) -> dict[str, dict[str, float]]:
    """For each connective category, the distribution over which connective the
    writer chooses. Only relevant categories with at least one occurrence are
    returned."""
    if not words:
        return {}
    out: dict[str, dict[str, float]] = {}
    for category, members in CONNECTIVES.items():
        counter = Counter(w for w in words if w in members)
        total = sum(counter.values())
        if total == 0:
            continue
        out[category] = {w: counter[w] / total for w in counter}
    return out


# ---------- Top-level entry ----------------------------------------------------

def extract_stats(
    text: str,
    nlp: spacy.Language,
    lexicons: Lexicons | None = None,
) -> dict[str, Any]:
    """Run all deterministic extractors and return a dict matching the schema's
    `stats` block."""
    text = normalize_text(text)
    if not text:
        return {}

    doc = nlp(text)
    words = lowercase_word_tokens(doc)
    word_count = len(words)

    out: dict[str, Any] = {
        "sentence_length": sentence_length_stats(doc),
        "paragraph_length_sentences": paragraph_length_stats_sentences(doc, text),
        "function_words_per_100w": function_words_per_100w(words),
        "pronoun_ratios": pronoun_ratios(words),
        "punctuation_per_100w": punctuation_per_100w(text, word_count),
        "hedge_per_100w": hedge_per_100w(words),
        "booster_per_100w": booster_per_100w(words),
        "stance_per_100w": stance_per_100w(words),
        "engagement_per_100w": engagement_per_100w(words),
        "contraction_rate": contraction_rate(text, word_count),
        "passive_voice_rate": passive_voice_rate(doc),
        "bullet_density": bullet_density(text),
        "lexical_diversity": lexical_diversity(words),
        "lexical_complexity": lexical_complexity(words, lexicons),
        "readability": readability(text, doc),
        "sentence_opener_pos_dist": sentence_opener_pos_dist(doc),
        "connective_prefs": connective_prefs(words),
    }

    # Prune empty sub-dicts so the output validates cleanly against a schema
    # that disallows empty {} via additionalProperties:false on inner shapes.
    return {k: v for k, v in out.items() if v not in ({}, [], None)}
