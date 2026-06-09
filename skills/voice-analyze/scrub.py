"""Scrub validator — privacy-preserving check on LLM-generated descriptors
and synthetic exemplars before they enter a proposal file.

Rules:
1. No long verbatim substring from the source (>5-word overlap rejected).
2. No proper nouns from the source (capitalized non-sentence-initial multi-letter
   tokens not present in a common allowlist).
3. No emails, URLs, @-handles, or hash-like identifiers.
4. No common explicit identifiers (numbers ≥5 digits that look like IDs).

On any rejection the whole proposal halts loudly with the failing item flagged
— the design choice is to surface near-misses rather than silently scrub, so
the human reviewer sees what almost slipped through and can fix the analyzer's
prompts if leaks become common.

This module is deterministic and lexicon-independent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Pull in the common-English lexicon (frozenset of ~5000 most-frequent
# English words from wordfreq). Used in two places below.
import sys
from pathlib import Path
_SKILL_DIR = Path(__file__).resolve().parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))
from lexicons import COMMON_ENGLISH  # noqa: E402

# ---------- Patterns -----------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\bhttps?://\S+\b", re.IGNORECASE)
# Negative lookbehind for alphanumerics to avoid matching the domain part of
# an email as if it were a handle (the existing _EMAIL_RE will catch the
# email separately and unambiguously).
_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9])@[A-Za-z][A-Za-z0-9_.-]{1,}")
_HEX_HASH_RE = re.compile(r"\b[a-f0-9]{32,}\b", re.IGNORECASE)
_LONG_DIGIT_RE = re.compile(r"\b\d{5,}\b")

# Handles that the LLM uses as placeholders (per our exemplar prompt) —
# @Alice, @Bob, etc. — should not flag as identifier leaks.
_PLACEHOLDER_HANDLE_NAMES: frozenset[str] = frozenset({
    "alice", "bob", "carol", "dave", "eve", "frank",
})

# Domains documented for use in fictional content (RFC 2606 / RFC 6761
# reserved + common fictional-corp patterns). Emails on these domains are
# not real-identity leaks even if the local-part looks like a name.
_FICTIONAL_EMAIL_DOMAIN_RE = re.compile(
    r"@(?:example\.(?:com|org|net)|examplecorp\.(?:com|org|net)|test\.(?:com|org|net))$",
    re.IGNORECASE,
)

# Placeholder URL domains the descriptor LLM uses when following the
# anti-leak rules — it sanitizes real URLs by swapping the host for one
# of these. Treating them as leaks would punish the LLM for doing the
# right thing. Includes RFC 2606 reserved (example.*), localhost, and
# the `internal.wiki` host the prompt encourages for fictional internal
# documentation URLs.
_FICTIONAL_URL_HOST_RE = re.compile(
    r"^https?://(?:"
    r"(?:[a-z0-9-]+\.)?example\.(?:com|org|net)"
    r"|(?:[a-z0-9-]+\.)?examplecorp\.(?:com|org|net)"
    r"|(?:[a-z0-9-]+\.)?test\.(?:com|org|net)"
    r"|internal\.wiki"
    r"|internal\.example"
    r"|localhost(?::\d+)?"
    r"|127\.0\.0\.1(?::\d+)?"
    r")(?:[/?#]|$)",
    re.IGNORECASE,
)


def _handle_is_placeholder(handle_text: str) -> bool:
    """True if @Alice / @bob / etc — a placeholder name the prompts encourage."""
    name = handle_text.lstrip("@").lower()
    return name in _PLACEHOLDER_HANDLE_NAMES


def _email_is_fictional(email_text: str) -> bool:
    """True if the email is on an obviously-fictional domain
    (example.com, examplecorp.com, test.com, etc.)."""
    return bool(_FICTIONAL_EMAIL_DOMAIN_RE.search(email_text))


def _url_is_fictional(url_text: str) -> bool:
    """True if the URL's host is a placeholder/fictional domain — the
    descriptor LLM uses these to sanitize real URLs in exemplars."""
    return bool(_FICTIONAL_URL_HOST_RE.match(url_text))

# Words that may be sentence-initial-capitalized but are common English and
# should not count as proper-noun leakage. Lowercase entries match the
# lowercased form of the suspected proper noun.
_PROPER_NOUN_ALLOWLIST: frozenset[str] = frozenset({
    # Filler / common
    "the", "a", "an", "i", "we", "you", "they", "he", "she", "it",
    "this", "these", "those", "that", "and", "but", "or", "if", "when",
    "where", "while", "because", "since", "as", "for", "in", "on", "of",
    "to", "with", "from", "by", "at", "into", "through", "during",
    # Months / days (and common abbreviations)
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    # Frequent placeholder-content words our exemplars may use
    "x", "y", "z", "a", "b", "c", "n", "m",
    # Placeholder NAMES the exemplar prompt explicitly tells the LLM to use.
    # These appearing in an exemplar is the LLM doing exactly what we asked
    # for, not a leak. (Matches the CLAUDE.md placeholder convention too.)
    "alice", "bob", "carol", "dave", "eve", "frank",
    # Generic-category placeholders the exemplar prompt explicitly encourages —
    # "Tool A", "Service B", "the Team", "the Feature", etc.
    "tool", "system", "service", "feature", "team", "application", "platform",
    "library", "framework", "database", "dashboard", "deployment", "release",
    "project", "environment", "component", "module",
    # English / generic
    "english", "american", "european",
    # Discourse markers and transition verbs that style-descriptions
    # frequently use mid-sentence ("Pivots from X to Y", "Swaps perspective").
    # These are common English used to describe rhetorical moves, NOT
    # proper-noun leakage.
    "pivot", "pivots", "swap", "swaps", "shift", "shifts",
    "beyond", "additionally", "furthermore", "moreover", "however",
    "overall", "finally", "ultimately", "specifically", "notably",
    "instead", "rather", "though", "although", "still",
    "always", "never", "often", "sometimes", "usually", "typically",
    "starts", "starting", "ends", "ending", "begins", "beginning",
    "opens", "opening", "closes", "closing",
    "frames", "framing", "leads", "leading", "uses", "using",
    "favors", "favoring", "prefers", "preferring",
    "describes", "describing", "names", "naming",
    "our", "ours", "us",
    # Common abstract-noun first words in style descriptions
    "section", "paragraph", "list", "bullet", "header", "heading",
    "summary", "description", "introduction", "conclusion",
    "argument", "argument", "thesis", "claim",
    # Document-type words that may appear in style descriptions without
    # being source-specific
    "design", "note", "review", "report", "memo", "draft", "outline",
    "proposal", "document", "documentation",
    # More discourse/clause-initial English commonly appearing in style
    # descriptions (came up empirically during propose-batch on real docs)
    "risks", "risk", "plus", "after", "another", "aside", "before",
    "during", "apart", "likewise", "consequently", "subsequently",
    "need", "needs", "start", "starts", "stop", "stops", "begin", "begins",
    "every", "each", "any", "some", "all", "most", "many", "few",
    "tradeoff", "tradeoffs", "options", "approaches", "alternatives",
    "rather", "instead", "given", "since", "while", "throughout",
    "first", "second", "third", "next", "lastly", "finally",
    # Imperative verbs common in outlines, demos, and walkthroughs —
    # appear as bullet-first verbs in style descriptions
    "show", "shows", "showing", "walk", "walks", "walking",
    "generate", "generates", "generating", "launch", "launches", "launching",
    "create", "creates", "creating", "build", "builds", "building",
    "demonstrate", "demonstrates", "demonstrating",
    "explore", "explores", "exploring", "explain", "explains", "explaining",
    "open", "opens", "opening", "run", "runs", "running",
    "load", "loads", "loading", "load", "click", "clicks", "clicking",
    "type", "types", "typing", "select", "selects", "selecting",
    "manipulate", "manipulates", "manipulating",
    "swap", "swapping",   # also covers demo-flow swaps
    "talk", "talks", "talking",
    "play", "plays", "playing",
    "step", "steps", "stepping",
    "call", "calls", "calling",
    "pivot",  # already in but ensures verb-form
    "skip", "skips", "skipping",
    "view", "views", "viewing",
    "include", "includes", "including",
    "highlight", "highlights", "highlighting",
    "introduce", "introduces", "introducing",
    "pause", "pauses", "pausing",
})

# Multi-letter, starts with uppercase, not entirely uppercase (allow acronyms via
# `--allow-acronyms` flag in the caller).
_CAP_WORD_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]*)*)\b")


# ---------- Result types -------------------------------------------------------

@dataclass
class ScrubFinding:
    rule: str
    snippet: str
    detail: str


@dataclass
class ScrubResult:
    findings: list[ScrubFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def add(self, rule: str, snippet: str, detail: str) -> None:
        self.findings.append(ScrubFinding(rule=rule, snippet=snippet, detail=detail))


# ---------- Helpers ------------------------------------------------------------

def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def _ngrams(words: list[str], n: int) -> list[tuple[str, ...]]:
    if len(words) < n:
        return []
    return [tuple(w.lower() for w in words[i : i + n]) for i in range(len(words) - n + 1)]


# ---------- Individual checks --------------------------------------------------

def check_no_email_url_handle(text: str, result: ScrubResult) -> None:
    for m in _EMAIL_RE.finditer(text):
        if _email_is_fictional(m.group(0)):
            continue
        result.add("identifier:email", m.group(0), "Email address detected")
    for m in _URL_RE.finditer(text):
        if _url_is_fictional(m.group(0)):
            continue
        result.add("identifier:url", m.group(0), "URL detected")
    for m in _HANDLE_RE.finditer(text):
        if _handle_is_placeholder(m.group(0)):
            continue
        result.add("identifier:handle", m.group(0), "@-handle detected")
    for m in _HEX_HASH_RE.finditer(text):
        result.add("identifier:hex_hash", m.group(0), "Hex hash detected")
    for m in _LONG_DIGIT_RE.finditer(text):
        result.add(
            "identifier:long_digit_id",
            m.group(0),
            f"Digit run of length {len(m.group(0))} (possible ID)",
        )


def check_no_long_substring_overlap(
    candidate: str,
    source: str,
    result: ScrubResult,
    n: int = 6,
    min_specific_words: int = 2,
) -> None:
    """Reject if any n-gram of candidate words appears in source AND the
    n-gram contains at least `min_specific_words` non-common-English words.

    The common-English density filter is the second layer of defense:
    matching a 6-word string of stock helper phrases (e.g. "we'd like to
    start by proposing", "this document is meant to capture") is not a
    leak signal — those phrases are normal English boilerplate that any
    writer might reproduce coincidentally. Real source leaks contain
    specific identifying terms (project names, technologies, etc.) that
    are NOT in the common-English lexicon.

    Default n=6 implements the "no >5-word verbatim quotes" rule;
    min_specific_words=2 means the ngram must carry at least two
    identifying terms to be flagged.
    """
    cand_words = _word_tokens(candidate)
    src_words = _word_tokens(source)
    if len(cand_words) < n or len(src_words) < n:
        return
    src_ngrams = set(_ngrams(src_words, n))
    for ng in _ngrams(cand_words, n):
        if ng not in src_ngrams:
            continue
        # Count words NOT in the common-English lexicon — these are the
        # identifying words that make the match meaningful.
        specific = sum(1 for w in ng if w not in COMMON_ENGLISH)
        if specific < min_specific_words:
            continue
        result.add(
            "leak:ngram_overlap",
            " ".join(ng),
            f"{n}-word substring matches source verbatim ({specific} specific words)",
        )
        return  # one finding is enough — caller will reject


def check_no_proper_nouns(
    candidate: str,
    result: ScrubResult,
    extra_allowlist: frozenset[str] = frozenset(),
) -> None:
    """Flag capitalized multi-letter words that aren't on the allowlist.

    Sentence-initial capitalization is identified by tracking the start of
    each "fresh sentence-like context":
    - position 0
    - immediately after sentence-final punctuation + whitespace ([.!?:] then \\s+)
    - start of a new line (after \\n, optionally past whitespace/bullets/list markers)

    This last case is important for multi-line descriptor blocks where the LLM
    emits bullet lists or line-separated items — without it, every list item's
    first capital looks like a mid-sentence proper-noun candidate.
    """
    allow = _PROPER_NOUN_ALLOWLIST | {w.lower() for w in extra_allowlist}
    text = candidate
    sentence_start_positions: set[int] = {0}
    # After sentence-final punctuation + optional closing quote + optional
    # markdown emphasis closer (`**`, `*`, `_`, backtick) + whitespace.
    # The emphasis closer handles cases like `**Reliability.** High-traffic`
    # where the period is inside the bold marker and the next sentence starts
    # right after.
    for m in re.finditer(r"[.!?](?:[\"')\]]+)?(?:[*_`]+)?\s+", text):
        sentence_start_positions.add(m.end())
    # After newline + optional whitespace + optional bullet/list marker
    for m in re.finditer(r"\n\s*(?:[-*+•]|\d+[.)])?\s*", text):
        sentence_start_positions.add(m.end())
    # After colon (with optional markdown-emphasis closer like `**`) plus
    # whitespace — handles "Pattern: Description starts here" and the
    # bold-colon case "**Ask:** Approve resourcing".
    # Also: each subsequent comma-space within the colon-introduced list,
    # up until the next sentence-terminator or newline.
    for m in re.finditer(r":(?:[*_`]+)?\s+", text):
        list_start = m.end()
        sentence_start_positions.add(list_start)
        pos = list_start
        end_match = re.search(r"[.!?\n]", text[pos:])
        list_end = pos + end_match.start() if end_match else len(text)
        for cm in re.finditer(r",\s+", text[pos:list_end]):
            sentence_start_positions.add(pos + cm.end())

    # After a markdown table cell separator `|`. Each cell starts a new
    # sentence-like context, so words right after `| ` are sentence-initial.
    for m in re.finditer(r"\|\s+", text):
        sentence_start_positions.add(m.end())

    # After a mid-line bullet/dash separator. LLM-generated content
    # sometimes uses `+ Item + Item - Item` or `Option X: + Foo - Bar` on a
    # single line as inline bullets. Also includes `·` (U+00B7 middle dot)
    # which the LLM emits frequently for short bullet lists. We require a
    # whitespace or sentence-end before the marker so mid-word hyphens
    # don't relax.
    for m in re.finditer(r"(?:^|[\s:.!?])\s*[+\-•·]\s+", text):
        sentence_start_positions.add(m.end())

    # After an opening quote (straight or smart). LLM frequently embeds
    # a quoted full sentence: `clarified directly: 'Linking against this
    # library does not require...'`. The quote opens a new sentence.
    for m in re.finditer(r"['\"‘“]\s*", text):
        sentence_start_positions.add(m.end())

    for m in _CAP_WORD_RE.finditer(text):
        word = m.group(1)
        word_lower = word.lower()
        if word_lower in allow:
            continue
        # Lexicon-backed common-English check. If the word is in the
        # top-5000-English-words list, it's almost certainly a common noun /
        # verb / adjective being capitalized for structural or emphasis
        # reasons, not a proper noun.
        if word_lower in COMMON_ENGLISH:
            continue
        # Sentence-initial? Skip — common English at sentence start.
        # We walk back through any markdown emphasis (`**`, `*`, `_`, `\``),
        # header markers (`#`), and whitespace to handle patterns like
        # "## Title", "**Bold**", "* **Reliability**", "- Add retry logic".
        # These structural prefixes don't change a word's sentence-initial-ness.
        if _is_sentence_initial(m.start(), text, sentence_start_positions):
            continue
        # All-caps short tokens are likely acronyms (API, SDK) — flag separately
        # via the long-digit/hex rules; here we only flag mixed-case proper nouns.
        if word.isupper():
            continue
        result.add("leak:proper_noun", word, "Capitalized proper-noun candidate")


# Characters that can appear between a known sentence-start and the actual
# CapWord. Markdown emphasis (`**`, `*`, `_`, `` ` ``), header markers (`#`),
# bullet markers (`-`, `+`, `•`, `·`, `>`), and whitespace.
# These are safe to skip during walk-back: they're structural prefixes, not
# content. Note: `-` is included because bullet lists use it, and hyphenated
# words have a letter (not `-`) immediately before their second capital, so
# this doesn't create false negatives for "Mid-Caps"-style words.
_EMPHASIS_CHARS = set("*_`#-+•·> \t")


def _is_sentence_initial(match_start: int, text: str, sentence_starts: set[int]) -> bool:
    r"""Return True if match_start is sentence-initial, walking back through
    structural markdown prefixes that don't change a word's sentence-initial
    nature (e.g. ``**``, ``*``, ``#``, ``_``, backtick, and intervening
    whitespace)."""
    if match_start in sentence_starts:
        return True
    pos = match_start
    while pos > 0 and text[pos - 1] in _EMPHASIS_CHARS:
        pos -= 1
        if pos in sentence_starts:
            return True
    return False


# ---------- Top-level entry ----------------------------------------------------

def scrub(
    candidate: str,
    source: str | None = None,
    extra_allowlist: frozenset[str] = frozenset(),
    ngram_threshold: int = 6,
) -> ScrubResult:
    """Run all checks against `candidate`.

    If `source` is provided, also check for verbatim substring overlap with it.
    Returns a ScrubResult; caller decides whether to halt on findings.
    """
    result = ScrubResult()
    check_no_email_url_handle(candidate, result)
    check_no_proper_nouns(candidate, result, extra_allowlist=extra_allowlist)
    if source:
        check_no_long_substring_overlap(candidate, source, result, n=ngram_threshold)
    return result
