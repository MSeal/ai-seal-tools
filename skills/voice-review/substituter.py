"""Substitute flagged content with placeholders before merging into profile.

When the reviewer accepts a flagged exemplar with substitution enabled,
this module rewrites the exemplar's synthetic / pattern / when_to_use
fields to replace name leaks with placeholders and specific numbers with
generic patterns. The substituted exemplar — not the original — gets
written to the live profile.

Two kinds of substitution:

1. **Name substitution**: any flagged proper-noun that appears in a
   people-context (attendee list, near other placeholder names, or after
   markers like "Attendees:", "By:", "From:") gets replaced with a
   placeholder name. First+last name patterns (placeholder name
   immediately followed by an unknown capitalized word) get the last
   name swapped with a placeholder surname like "Example".

2. **Number substitution**: years (19xx, 20xx), dates with month names,
   and 2+ digit standalone numbers get replaced with generic placeholders
   so specific quantities and timestamps don't leak.

`should_substitute` decides which approach (substitute vs accept-as-false-positive)
based on COMMON_ENGLISH membership and people-context detection. The
reviewer overrides via the `substitute:` field in the review file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent / "voice-analyze"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))
from lexicons import COMMON_ENGLISH  # noqa: E402

# Placeholders the LLM is already prompted to use — also our targets when
# substituting flagged names. Keeping these consistent across the codebase.
PLACEHOLDER_FIRST_NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]
PLACEHOLDER_LAST_NAMES = ["Example", "Sample", "Demo", "Test", "Placeholder", "Generic"]

# Markers that strongly indicate a people-context (attendees, reviewers,
# author/sender headers). When a flagged name appears within this many
# chars of one of these markers, treat it as a name leak.
_PEOPLE_CONTEXT_MARKERS = [
    "attendees:",
    "attendees\n",
    "reviewers:",
    "by:",
    "from:",
    "to:",
    "author:",
    "presenter:",
    "owners:",
    "owner:",
    "co-author:",
    "participants:",
    "@",  # @Alice etc. usually denotes person mentions
]
_PEOPLE_CONTEXT_WINDOW = 100


def in_people_context(text: str, word: str) -> bool:
    """Return True if `word` appears after an explicit people-context
    marker like "Attendees:", "By:", "Reviewers:", "@", etc.

    This is the broad-context check. The narrow check (`in_strong_name_context`)
    handles the "Bob Marquez" immediate-adjacency case. Don't try to infer
    name-ness from same-line proximity alone — bullet lists and label
    brackets share lines with placeholders too, e.g.
        "Critical Path Items [P0] · Validate ... before Alice's review"
        "Current accounts: [Active] Alice Co., [Scheduling] Bob Industries"
    where Validate / Scheduling are clearly section labels, not names.
    """
    word_lower = word.lower()
    text_lower = text.lower()
    for m in re.finditer(r"\b" + re.escape(word_lower) + r"\b", text_lower):
        window_start = max(0, m.start() - _PEOPLE_CONTEXT_WINDOW)
        window = text_lower[window_start : m.start()]
        if any(marker in window for marker in _PEOPLE_CONTEXT_MARKERS):
            return True
    return False


def in_strong_name_context(text: str, word: str) -> bool:
    """Return True if `word` is IMMEDIATELY adjacent to a placeholder first
    name (within 1-2 tokens). This is the "Bob Marquez" / "Alice Chen"
    pattern — strong enough to override the COMMON_ENGLISH lexicon check,
    because many common surnames (Chen, Smith, Wong, Garcia, Kim, etc.)
    appear in word-frequency lists but are clearly name leaks when
    adjacent to a placeholder."""
    # Pattern: <placeholder> <word>  OR  <word> <placeholder>
    pattern = re.compile(
        r"\b(?:" + "|".join(PLACEHOLDER_FIRST_NAMES) + r")\s+" + re.escape(word) + r"\b"
        + r"|"
        + r"\b" + re.escape(word) + r"\s+(?:" + "|".join(PLACEHOLDER_FIRST_NAMES) + r")\b",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def should_substitute_name(text: str, word: str) -> bool:
    """Decide whether a flagged name token warrants substitution.

    - If the word is adjacent to a placeholder first name (Alice/Bob/...)
      → substitute. This catches `Bob Marquez` style surnames even when
      the surname is in COMMON_ENGLISH (Chen, Smith, etc.).
    - Otherwise, if the word is in COMMON_ENGLISH → false positive
      (e.g. "Retro", "Backend"); don't substitute.
    - Otherwise, if the word appears in a people-context (attendee
      list, "By:" header, etc.) → substitute.
    - Otherwise → leave for human review (return False).
    """
    if in_strong_name_context(text, word):
        return True
    if word.lower() in COMMON_ENGLISH:
        return False
    return in_people_context(text, word)


def substitute_text(
    text: str,
    flagged_words: list[str],
    substitute_numbers: bool = True,
) -> tuple[str, dict[str, str]]:
    """Apply substitutions to `text`. Returns (modified_text, mapping).

    `flagged_words` are the proper-noun snippets the scrub flagged. Only
    those determined to be name leaks (per `should_substitute_name`) get
    substituted with placeholder names.

    When `substitute_numbers=True`, also replaces years and standalone
    2+ digit numbers with generic placeholders.
    """
    new_text = text
    mapping: dict[str, str] = {}
    placeholder_rotation = 0

    # First pass: detect "FirstName LastName" patterns — placeholder first
    # name immediately followed by a flagged word. Replace the last name
    # with a placeholder surname.
    for word in flagged_words:
        if word in mapping:
            continue
        if not should_substitute_name(text, word):
            continue
        # Check for "Placeholder LastName" pattern
        pattern = re.compile(
            r"\b(" + "|".join(PLACEHOLDER_FIRST_NAMES) + r")\s+" + re.escape(word) + r"\b",
            re.IGNORECASE,
        )
        first_name_match = pattern.search(new_text)
        if first_name_match:
            first_name = first_name_match.group(1)
            idx = next(
                (i for i, n in enumerate(PLACEHOLDER_FIRST_NAMES) if n.lower() == first_name.lower()),
                0,
            )
            replacement = PLACEHOLDER_LAST_NAMES[idx % len(PLACEHOLDER_LAST_NAMES)]
            mapping[word] = replacement
            new_text = re.sub(r"\b" + re.escape(word) + r"\b", replacement, new_text)
            continue

        # Standalone name → rotate through first-name placeholders
        # (avoiding duplicates within the same text)
        existing_placeholders = set(mapping.values())
        for i in range(len(PLACEHOLDER_FIRST_NAMES)):
            candidate = PLACEHOLDER_FIRST_NAMES[(placeholder_rotation + i) % len(PLACEHOLDER_FIRST_NAMES)]
            if candidate not in existing_placeholders:
                mapping[word] = candidate
                placeholder_rotation = (placeholder_rotation + i + 1) % len(PLACEHOLDER_FIRST_NAMES)
                break
        else:
            mapping[word] = PLACEHOLDER_FIRST_NAMES[placeholder_rotation % len(PLACEHOLDER_FIRST_NAMES)]
            placeholder_rotation += 1
        new_text = re.sub(r"\b" + re.escape(word) + r"\b", mapping[word], new_text)

    # Number substitution
    if substitute_numbers:
        # Years (4-digit 19xx-20xx)
        new_text = re.sub(r"\b(?:19|20)\d{2}\b", "<YYYY>", new_text)
        # Standalone 2+ digit numbers → <N> (preserves single digits like "1, 2, 3")
        # Avoid replacing numbers that are part of compound tokens like "v2", "k8s"
        new_text = re.sub(r"(?<![A-Za-z_])\d{2,}(?![A-Za-z_])", "<N>", new_text)

    return new_text, mapping


def substitute_exemplar(
    exemplar: dict,
    substitute_numbers: bool = True,
) -> tuple[dict, dict[str, str]]:
    """Apply substitutions to all text fields of an exemplar.

    Returns (new_exemplar, combined_mapping). The original `exemplar` dict
    is not mutated.
    """
    new = dict(exemplar)
    flagged_words = []
    for f in exemplar.get("scrub_findings", []):
        if f.get("rule") == "leak:proper_noun":
            flagged_words.append(f["snippet"])

    combined: dict[str, str] = {}
    for field in ("synthetic", "pattern", "when_to_use"):
        if field not in new:
            continue
        new_value, m = substitute_text(new[field], flagged_words, substitute_numbers=substitute_numbers)
        new[field] = new_value
        combined.update(m)
    return new, combined
