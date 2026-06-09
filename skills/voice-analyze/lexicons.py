"""Static word lists and lexicon loaders for the voice-analyze stats extractor.

Inline word lists (function words, pronouns by group, hedges, boosters, stance,
engagement, connectives, Latinate suffixes) live here as Python data — small
enough to be inspected and edited directly, no external dependency.

External lexicons (Brysbaert concreteness norms, optional Dale-Chall easy list)
are loaded lazily from `~/.cache/ai-seal-tools/voice/`. If a lexicon file is
absent the dependent stats are skipped from output — never a hard error.

The Brysbaert installer is documented in SETUP.md. We do NOT auto-fetch from a
URL inside the extractor because (a) the canonical XLSX format would add a
parsing dependency for every analyzer run, (b) the user explicitly asked for
this to be an install artifact, not a checked-in file. The user runs
`utils/install_voice_lexicons.py` once; subsequent analyzer runs are offline.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

CACHE_DIR = Path.home() / ".cache" / "ai-seal-tools" / "voice"
BRYSBAERT_CACHE = CACHE_DIR / "brysbaert_concreteness.tsv"
DALE_CHALL_CACHE = CACHE_DIR / "dale_chall_easy.txt"

# Common-English word list used by the scrub validator. Generated from
# wordfreq's top-N English words (see utils/build_common_english.py).
# Checked into the repo so scrub.py doesn't need a runtime dep on
# wordfreq. Used in two places:
# 1. Proper-noun check: if a capitalized word's lowercased form is in this
#    set, it's almost certainly not a proper noun and is allowed.
# 2. N-gram overlap check: a 6+ word verbatim match is only flagged if the
#    ngram contains ≥2 words NOT in this set (i.e. specific identifying
#    words, not stock helper phrases).
COMMON_ENGLISH_PATH = Path(__file__).resolve().parent / "data" / "common_english_words.txt"


def _load_common_english() -> frozenset[str]:
    if not COMMON_ENGLISH_PATH.is_file():
        return frozenset()
    words = set()
    for line in COMMON_ENGLISH_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.add(line.lower())
    return frozenset(words)


COMMON_ENGLISH: frozenset[str] = _load_common_english()


# Mirrors the keys of the profile template's audience_registry. The schema
# test (test_audience_registry_has_expected_initial_tags) keeps these in sync;
# if you change one, change the other and re-run tests.
VALID_AUDIENCES: frozenset[str] = frozenset({
    "technical_peer",
    "leadership",
    "direct_report",
    "cross_functional",
    "external_public",
    "casual",
    "self_notes",
})

VALID_DOC_TYPES: frozenset[str] = frozenset({"polished", "draft", "outline", "chat"})


# ---------- Inline lexicons ----------------------------------------------------

# Top ~50 English function words (closed-class items). Their frequencies are
# topic-independent and stable per-author — the stylometry workhorse. Lowercase
# keys; tokens are lowercased before lookup.
FUNCTION_WORDS: frozenset[str] = frozenset({
    "the", "of", "and", "to", "a", "in", "that", "it", "is", "was",
    "for", "with", "as", "on", "by", "at", "from", "but", "or", "not",
    "this", "these", "those", "be", "are", "were", "been", "being",
    "have", "has", "had", "do", "does", "did", "would", "could", "should",
    "will", "shall", "may", "might", "must", "can", "if", "then", "than",
    "so", "because", "since", "while", "when", "where", "how", "what",
    "which", "who", "whom", "whose", "an",
})


# Pronouns grouped by person/role. Ratios across these groups are voice-defining
# (the I-heavy writer, the we-heavy writer, the you-engaging writer).
PRONOUN_GROUPS: dict[str, frozenset[str]] = {
    "i_me": frozenset({"i", "me", "my", "mine", "myself"}),
    "we_us": frozenset({"we", "us", "our", "ours", "ourselves"}),
    "you_your": frozenset({"you", "your", "yours", "yourself", "yourselves"}),
    "they_them": frozenset({"they", "them", "their", "theirs", "themselves"}),
    "third_singular": frozenset({"he", "she", "him", "her", "his", "hers",
                                  "it", "its", "himself", "herself", "itself"}),
}


# Hedge markers — soften assertion, signal uncertainty.
HEDGES: frozenset[str] = frozenset({
    "maybe", "perhaps", "probably", "possibly", "somewhat", "fairly",
    "rather", "quite", "kind", "sort",  # "kind of", "sort of" — bigrams handled below
    "might", "may", "could", "would", "likely", "seems", "appears",
    "suggest", "suggests", "suppose", "supposed", "assume", "assumes",
    "tend", "tends", "generally", "typically", "usually", "often",
    "approximately", "roughly", "around", "almost",
})


# Booster markers — strengthen assertion. Counterpart to hedges.
BOOSTERS: frozenset[str] = frozenset({
    "clearly", "obviously", "definitely", "certainly", "indeed",
    "undoubtedly", "naturally", "absolutely", "completely", "totally",
    "always", "never", "must", "essentially", "fundamentally",
    "unquestionably", "evidently",
})


# Stance markers — attitudinal/evaluative adverbials.
STANCE: frozenset[str] = frozenset({
    "unfortunately", "fortunately", "surprisingly", "interestingly",
    "remarkably", "importantly", "notably", "frankly", "honestly",
    "regrettably", "thankfully", "sadly", "happily", "ironically",
    "predictably", "curiously",
})


# Engagement markers — direct reader address / invitation.
ENGAGEMENT: frozenset[str] = frozenset({
    "consider", "imagine", "suppose", "note", "recall", "remember",
    "see", "look", "notice", "observe", "think", "picture",
    # second-person pronouns counted separately in pronoun_ratios; engagement
    # focuses on the imperatives/invitations specifically
})


# Connective bigrams/words grouped by relation type.
CONNECTIVES: dict[str, frozenset[str]] = {
    "contrast": frozenset({
        "but", "however", "though", "although", "yet", "still",
        "nevertheless", "nonetheless", "whereas", "while", "conversely",
    }),
    "additive": frozenset({
        "and", "also", "furthermore", "moreover", "additionally", "plus",
        "besides", "likewise", "similarly",
    }),
    "causal": frozenset({
        "because", "since", "so", "therefore", "thus", "hence",
        "consequently", "accordingly", "as",  # "as" is causal-ish; ambiguous
    }),
    "temporal": frozenset({
        "then", "next", "after", "before", "when", "once", "finally",
        "eventually", "subsequently", "meanwhile",
    }),
}


# Latinate suffixes — words ending in these are predominantly Latin/Greek-rooted
# and signal a "fancy" register. Specifically chosen to exclude technical jargon
# (Kafka, broker, schema, OAuth, librdkafka don't carry these suffixes).
LATINATE_SUFFIXES: tuple[str, ...] = (
    "tion", "sion", "ity", "ate", "ize", "ise", "ify", "fy",
    "ence", "ance", "ous", "ive", "ment", "ology", "ography",
)


# Punctuation characters tracked individually. Em-dash and en-dash matter for
# voice — Claude over-uses em-dashes ~3x the human baseline so we calibrate
# against the writer's actual rate.
TRACKED_PUNCTUATION: dict[str, str] = {
    "em_dash": "—",
    "en_dash": "–",
    "hyphen": "-",
    "parenthesis": "(",  # open-paren count = total paren pairs (heuristic)
    "semicolon": ";",
    "colon": ":",
    "exclamation": "!",
    "question": "?",
    "ellipsis": "…",
}


# ---------- External lexicons (lazy load) -------------------------------------

@dataclass
class Lexicons:
    """Container for optional external lexicons. Fields are None if the lexicon
    file is not installed; the extractor skips those metrics from output."""
    concreteness: dict[str, float] | None = field(default=None)
    dale_chall_easy: frozenset[str] | None = field(default=None)


def load_lexicons() -> Lexicons:
    return Lexicons(
        concreteness=_load_brysbaert(),
        dale_chall_easy=_load_dale_chall(),
    )


def _load_brysbaert() -> dict[str, float] | None:
    """Load Brysbaert et al. (2014) concreteness norms if cached locally.

    Expected format: TSV with header `Word\tBigram\tConc.M\tConc.SD\tUnknown\tTotal\tPercent_known\tSUBTLEX\tDom_Pos`.
    Only unigrams (Bigram=0) with non-empty Conc.M are loaded.

    Returns None if the lexicon is not installed — extractor skips concreteness
    in that case.
    """
    if not BRYSBAERT_CACHE.is_file():
        return None
    out: dict[str, float] = {}
    with BRYSBAERT_CACHE.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                # Reject bigrams; we only score single-word lookups
                if int(row.get("Bigram", "0") or 0) == 1:
                    continue
                word = (row.get("Word") or "").strip().lower()
                if not word:
                    continue
                score = float(row.get("Conc.M") or 0)
                if score == 0:
                    continue
                out[word] = score
            except (ValueError, TypeError):
                continue
    return out if out else None


def _load_dale_chall() -> frozenset[str] | None:
    """Load Dale-Chall easy-word list (or any equivalent simple-word reference).

    Expected format: one lowercase word per line, # comments allowed. Optional;
    if absent, the extractor reports lexical_complexity without dale_chall_simple_pct.
    """
    if not DALE_CHALL_CACHE.is_file():
        return None
    words: set[str] = set()
    for line in DALE_CHALL_CACHE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.add(line.lower())
    return frozenset(words) if words else None


def stem_for_lookup(word: str) -> Iterable[str]:
    """Yield the word and a few light morphological variants for lexicon lookup.

    Brysbaert stores base forms; "implementations" must fall back to
    "implementation" to be found. We try the original, plural stripping, and
    common verb-form stripping. Order matters — first match wins at the call
    site.
    """
    w = word.lower()
    yield w
    # Plural / -s
    if w.endswith("s") and len(w) > 3:
        yield w[:-1]
    # -es (boxes -> box)
    if w.endswith("es") and len(w) > 4:
        yield w[:-2]
    # -ies (parties -> party)
    if w.endswith("ies") and len(w) > 4:
        yield w[:-3] + "y"
    # -ed (implemented -> implement)
    if w.endswith("ed") and len(w) > 4:
        yield w[:-2]
        if w.endswith("ied") and len(w) > 4:
            yield w[:-3] + "y"
    # -ing (running -> run, implementing -> implement)
    if w.endswith("ing") and len(w) > 5:
        yield w[:-3]
        # double-consonant case: "running" -> "run"
        if len(w) > 5 and w[-4] == w[-5]:
            yield w[:-4]
    # -ly (clearly -> clear)
    if w.endswith("ly") and len(w) > 4:
        yield w[:-2]
