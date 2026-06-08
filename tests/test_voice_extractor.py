"""Unit tests for the deterministic stats extractor.

The spaCy `en_core_web_sm` model is required for POS-dependent tests
(passive voice, sentence opener distribution). If the model isn't installed,
those tests skip cleanly.
"""
from __future__ import annotations

import pytest
import spacy

import extractor
from extractor import (
    bullet_density,
    connective_prefs,
    contraction_rate,
    extract_stats,
    function_words_per_100w,
    hedge_per_100w,
    is_bullet_line,
    lexical_complexity,
    lexical_diversity,
    normalize_text,
    pronoun_ratios,
    punctuation_per_100w,
    split_paragraphs,
    stance_per_100w,
)
from lexicons import Lexicons


@pytest.fixture(scope="session")
def nlp():
    """Load the real en_core_web_sm model once per test session.

    Skip POS-requiring tests if it isn't installed (CI environments).
    """
    try:
        return spacy.load("en_core_web_sm")
    except OSError:
        pytest.skip(
            "en_core_web_sm not installed. Install with:\n"
            "  UV_NO_CONFIG=1 uv run python -m spacy download en_core_web_sm"
        )


@pytest.fixture(scope="session")
def blank_nlp():
    """Tokenizer-only pipeline for tests that don't need POS."""
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    return nlp


# ---------- Normalization ----------

def test_normalize_collapses_internal_whitespace():
    assert normalize_text("a   b  c") == "a b c"


def test_normalize_keeps_paragraph_breaks():
    assert normalize_text("para1\n\npara2") == "para1\n\npara2"


def test_normalize_collapses_excess_newlines():
    assert normalize_text("para1\n\n\n\npara2") == "para1\n\npara2"


def test_normalize_unicode_nfc():
    # 'é' as decomposed (e + combining acute) should normalize to composed form
    assert normalize_text("café") == "café"


def test_split_paragraphs_two_paragraphs():
    paras = split_paragraphs("first paragraph.\n\nsecond paragraph.")
    assert paras == ["first paragraph.", "second paragraph."]


def test_split_paragraphs_handles_extra_whitespace():
    paras = split_paragraphs("first.\n   \n\nsecond.")
    assert paras == ["first.", "second."]


# ---------- Bullet density ----------

@pytest.mark.parametrize("line,expected", [
    ("- item", True),
    ("* item", True),
    ("+ item", True),
    ("• item", True),
    ("1. item", True),
    ("1) item", True),
    ("    - nested", True),
    ("plain prose", False),
    ("Heading", False),
    ("1 was a year", False),  # no separator after number
])
def test_is_bullet_line(line, expected):
    assert is_bullet_line(line) is expected


def test_bullet_density_all_bullets():
    text = "- one\n- two\n- three"
    assert bullet_density(text) == 1.0


def test_bullet_density_mixed():
    text = "Some prose.\n\n- a bullet\n- another bullet"
    # 3 non-blank lines, 2 bullets
    assert bullet_density(text) == pytest.approx(2 / 3)


def test_bullet_density_no_bullets():
    assert bullet_density("just\nprose\nlines") == 0.0


# ---------- Function words ----------

def test_function_words_per_100w_basic():
    words = ["the", "cat", "and", "the", "dog", "ran"]
    # 'the' x2, 'and' x1 = 3 function words / 6 total * 100 = 50/100 each split
    result = function_words_per_100w(words)
    assert result["the"] == pytest.approx(2 * 100 / 6)
    assert result["and"] == pytest.approx(1 * 100 / 6)
    assert "cat" not in result


def test_function_words_empty():
    assert function_words_per_100w([]) == {}


# ---------- Pronoun ratios ----------

def test_pronoun_ratios_i_heavy():
    words = ["i", "think", "i", "should", "say", "i"]
    ratios = pronoun_ratios(words)
    assert ratios["i_me"] == pytest.approx(3 / 6)
    assert ratios["we_us"] == 0.0
    assert ratios["you_your"] == 0.0


def test_pronoun_ratios_engagement():
    words = ["you", "should", "see", "your", "code"]
    ratios = pronoun_ratios(words)
    assert ratios["you_your"] == pytest.approx(2 / 5)


# ---------- Punctuation ----------

def test_punctuation_em_dash_counted():
    text = "Tradeoff — X gives A but B — Y the reverse."
    # 10 words, 2 em-dashes
    result = punctuation_per_100w(text, word_count=10)
    assert result["em_dash"] == pytest.approx(20.0)


def test_punctuation_distinguishes_em_dash_from_hyphen():
    text = "fast-paced text — with an em dash"
    # Just verify both are tracked separately and nonzero
    result = punctuation_per_100w(text, word_count=8)
    assert result["em_dash"] > 0
    assert result["hyphen"] > 0


def test_punctuation_empty_text():
    assert punctuation_per_100w("", word_count=0) == {}


# ---------- Hedges / boosters / stance ----------

def test_hedge_rate():
    words = ["this", "might", "probably", "work", "i", "think"]
    # might + probably + (think not in HEDGES) = 2 hedges / 6 words * 100
    assert hedge_per_100w(words) == pytest.approx(2 * 100 / 6)


def test_stance_marker_detected():
    words = ["unfortunately", "the", "test", "failed"]
    assert stance_per_100w(words) == pytest.approx(25.0)


# ---------- Contractions ----------

def test_contraction_rate_basic():
    text = "I'm sure we'll get it done, won't we?"
    # 9 words, 3 contractions
    assert contraction_rate(text, word_count=9) == pytest.approx(3 / 9)


def test_contraction_rate_no_contractions():
    text = "I am sure we will get it done"
    assert contraction_rate(text, word_count=8) == 0.0


# ---------- Lexical diversity ----------

def test_lexical_diversity_short_text_falls_back_to_ttr():
    words = ["a", "b", "c", "d", "e"] * 5  # 25 words, 5 types
    result = lexical_diversity(words)
    # Too short for MTLD; we return type count + simple TTR
    assert "moving_ttr" in result
    assert result["moving_ttr"] == pytest.approx(5 / 25)


def test_lexical_diversity_long_text():
    # 100 distinct words → high MTLD
    words = [f"word{i}" for i in range(100)]
    result = lexical_diversity(words)
    assert result["mtld"] > 0
    assert result["moving_ttr"] == pytest.approx(1.0)  # all unique within any window


# ---------- Lexical complexity ----------

def test_lexical_complexity_basic_no_lexicons():
    words = ["simple", "test", "of", "implementation", "modernization"]
    result = lexical_complexity(words, lexicons=None)
    assert "avg_word_length_chars" in result
    assert "avg_word_syllables" in result
    assert "pct_long_words" in result
    assert "latinate_ratio" in result
    # implementation + modernization are Latinate (-tion, -ation)
    assert result["latinate_ratio"] == pytest.approx(2 / 5)
    # Long words (>=7 chars): implementation (14), modernization (13) = 2
    assert result["pct_long_words"] == pytest.approx(2 / 5)
    # No lexicon → no dale_chall or concreteness
    assert "dale_chall_simple_pct" not in result
    assert "concreteness_score" not in result


def test_lexical_complexity_latinate_excludes_tech_jargon():
    # Tech terms shouldn't be flagged as Latinate (the design fix vs Dale-Chall)
    words = ["kafka", "broker", "schema", "topic", "partition", "consumer"]
    result = lexical_complexity(words, lexicons=None)
    # 'consumer' ends in 'er' — not Latinate. 'partition' ends in 'tion' — Latinate.
    # Only partition matches the suffix list. So 1/6.
    assert result["latinate_ratio"] == pytest.approx(1 / 6)


def test_lexical_complexity_with_dale_chall_lexicon():
    words = ["cat", "dog", "implementation"]
    lexicons = Lexicons(
        concreteness=None,
        dale_chall_easy=frozenset({"cat", "dog"}),
    )
    result = lexical_complexity(words, lexicons=lexicons)
    assert result["dale_chall_simple_pct"] == pytest.approx(2 / 3)


def test_lexical_complexity_concreteness_skipped_on_low_coverage():
    # 1 of 10 alphabetic words rated — coverage = 0.1, below threshold of 0.4
    jargon = ["kafka", "broker", "schema", "topic", "partition", "consumer", "producer", "offset", "librdkafka"]
    words = ["cat"] + jargon
    lexicons = Lexicons(
        concreteness={"cat": 5.0},
        dale_chall_easy=None,
    )
    result = lexical_complexity(words, lexicons=lexicons)
    assert "concreteness_score" not in result


def test_lexical_complexity_concreteness_emitted_on_sufficient_coverage():
    # 5 of 5 words rated — full coverage
    words = ["cat", "dog", "rock", "tree", "running"]
    lexicons = Lexicons(
        concreteness={"cat": 5.0, "dog": 5.0, "rock": 5.0, "tree": 5.0, "running": 4.0},
        dale_chall_easy=None,
    )
    result = lexical_complexity(words, lexicons=lexicons)
    assert "concreteness_score" in result
    assert result["concreteness_score"] == pytest.approx(4.8)


def test_lexical_complexity_concreteness_uses_stemming():
    # 'implementations' should fall back to 'implementation' via stemmer
    words = ["implementations", "implementing"]
    lexicons = Lexicons(
        concreteness={"implementation": 2.5, "implement": 2.6},
        dale_chall_easy=None,
    )
    result = lexical_complexity(words, lexicons=lexicons)
    assert "concreteness_score" in result
    # Both words resolve via stemming; mean of 2.5 + 2.6
    assert result["concreteness_score"] == pytest.approx(2.55)


# ---------- Connective preferences ----------

def test_connective_prefs_contrast_distribution():
    words = ["but", "but", "however", "and", "also"]
    result = connective_prefs(words)
    # 2 'but' + 1 'however' in contrast → but=0.66, however=0.33
    assert result["contrast"]["but"] == pytest.approx(2 / 3)
    assert result["contrast"]["however"] == pytest.approx(1 / 3)
    # additive has 'and', 'also'
    assert result["additive"]["and"] == pytest.approx(0.5)
    assert result["additive"]["also"] == pytest.approx(0.5)


def test_connective_prefs_skips_empty_categories():
    words = ["the", "cat", "ran"]
    result = connective_prefs(words)
    assert result == {}


# ---------- POS-dependent (require spaCy model) ----------

def test_passive_voice_rate(nlp):
    text = "The dog chased the cat. The cat was chased by the dog. The team built the system."
    doc = nlp(text)
    rate = extractor.passive_voice_rate(doc)
    # 1 of 3 sentences is passive
    assert rate == pytest.approx(1 / 3, abs=0.05)


def test_sentence_opener_pos_dist(nlp):
    text = (
        "The team decided to proceed. "
        "However, the timeline shifted. "
        "Considering the tradeoffs, we paused. "
        "Because of the freeze, we waited. "
        "But the deploy ran anyway."
    )
    doc = nlp(text)
    dist = extractor.sentence_opener_pos_dist(doc)
    # 5 sentences: DET (the=subject_np), ADV (however=adverbial),
    # VBG (considering=gerund), SCONJ (because=subordinator), CCONJ (but=conjunction)
    assert sum(dist.values()) == pytest.approx(1.0)
    assert "subject_np" in dist
    assert "adverbial" in dist
    assert "subordinator" in dist
    assert "conjunction" in dist


# ---------- Top-level entry ----------

def test_extract_stats_empty():
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    assert extract_stats("", nlp) == {}


def test_extract_stats_returns_all_expected_blocks(nlp):
    text = (
        "This is a short sample. It has two sentences, both straightforward.\n\n"
        "The second paragraph adds some hedging — maybe a bit of nuance, perhaps."
    )
    out = extract_stats(text, nlp, lexicons=None)
    expected_keys = {
        "sentence_length",
        "paragraph_length_sentences",
        "function_words_per_100w",
        "pronoun_ratios",
        "punctuation_per_100w",
        "hedge_per_100w",
        "booster_per_100w",
        "stance_per_100w",
        "engagement_per_100w",
        "contraction_rate",
        "passive_voice_rate",
        "bullet_density",
        "lexical_diversity",
        "lexical_complexity",
        "readability",
        "sentence_opener_pos_dist",
    }
    assert expected_keys.issubset(out.keys())


def test_extract_stats_validates_against_schema(nlp):
    """Stats output must conform to the v1 profile schema's stats sub-shape.

    We embed the extracted stats inside a minimal valid profile and validate
    the whole thing — catches any field name mismatch.
    """
    import schema as voice_schema

    text = (
        "We considered several approaches before settling on the simplest. "
        "The team agreed quickly. However, edge cases remain."
    )
    stats = extract_stats(text, nlp, lexicons=None)

    profile = {
        "schema_version": 1,
        "generated_at": "2026-05-24T10:00:00Z",
        "last_updated": "2026-05-24T10:00:00Z",
        "audience_registry": {"technical_peer": "test"},
        "audiences": {
            "technical_peer": {
                "description": "test",
                "sources_count": 1,
                "axis_baseline": {
                    "formality": 0.5,
                    "technical_density": 0.5,
                    "brevity": 0.5,
                    "warmth": 0.5,
                },
                "types": {
                    "polished": {
                        "sources_count": 1,
                        "stats": stats,
                        "descriptors": {},
                        "exemplars": [],
                    }
                },
            }
        },
        "shared_anti_patterns": [],
        "merge_history": [],
    }
    voice_schema.validate("profile", profile)
