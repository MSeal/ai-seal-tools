"""Unit tests for the scrub validator.

The scrub validator is the privacy guard between LLM-generated content and
the proposal files. These tests cover the failure modes we explicitly
designed against: email/URL/handle leakage, hex hash leakage, long digit
runs, verbatim n-gram overlap with the source, and proper-noun leakage.
"""
from __future__ import annotations

from scrub import (
    check_no_email_url_handle,
    check_no_long_substring_overlap,
    check_no_proper_nouns,
    scrub,
    ScrubResult,
)


# ---------- Email / URL / handle / hash ----------

def test_email_detected():
    """Real-domain email (not fictional) should flag."""
    result = scrub("Contact carol@confluent.io for details.")
    assert not result.passed
    assert any(f.rule == "identifier:email" for f in result.findings)


def test_url_detected():
    result = scrub("See https://example.com/path for more.")
    assert not result.passed
    assert any(f.rule == "identifier:url" for f in result.findings)


def test_handle_detected():
    result = scrub("Discussed with @someuser yesterday.")
    assert not result.passed
    assert any(f.rule == "identifier:handle" for f in result.findings)


def test_hex_hash_detected():
    result = scrub("Commit 0123456789abcdef0123456789abcdef.")
    assert not result.passed
    assert any(f.rule == "identifier:hex_hash" for f in result.findings)


def test_long_digit_run_detected():
    result = scrub("Issue 12345678 is closed.")
    assert not result.passed
    assert any(f.rule == "identifier:long_digit_id" for f in result.findings)


def test_short_digit_passes():
    # 4-digit number is not flagged — too common (year, version)
    result = scrub("In 2026 we shipped v3.")
    assert result.passed


def test_clean_text_passes():
    result = scrub("The tradeoff is X vs Y — X gives you A but B.")
    assert result.passed, [f.__dict__ for f in result.findings]


# ---------- Verbatim n-gram overlap ----------

def test_long_substring_overlap_rejected():
    """A 6+ word match with multiple specific identifiers should flag."""
    source = "The DTX team consumes the Kafka stream via librdkafka and writes to a Druid metrics store."
    candidate = "Recently the DTX team consumes the Kafka stream via librdkafka and writes downstream."
    result = scrub(candidate, source=source)
    assert any(f.rule == "leak:ngram_overlap" for f in result.findings), (
        f"Multi-specific-word overlap should flag; got {[(f.rule, f.snippet) for f in result.findings]}"
    )


def test_short_substring_overlap_allowed():
    source = "The DTX team wishes to consume the stream"
    # 3-word overlap is allowed under the default n=6 threshold
    candidate = "The team wishes to keep things simple."
    result = scrub(candidate, source=source)
    assert all(f.rule != "leak:ngram_overlap" for f in result.findings)


def test_substring_check_skipped_without_source():
    candidate = "Some perfectly clean synthetic text used as an exemplar."
    result = scrub(candidate, source=None)
    assert result.passed


# ---------- Proper-noun leakage ----------

def test_proper_noun_in_middle_of_sentence_flagged():
    candidate = "We discussed the proposal with Confluent's engineering team."
    result = scrub(candidate)
    assert not result.passed
    assert any(f.rule == "leak:proper_noun" and f.snippet == "Confluent" for f in result.findings)


def test_bullet_list_item_start_not_flagged():
    """Each bullet's first content word should be treated as sentence-initial."""
    candidate = "- Pivots from problem to solution\n- Swaps perspective mid-paragraph"
    result = scrub(candidate)
    proper_noun_findings = [f for f in result.findings if f.rule == "leak:proper_noun"]
    # "Pivots" and "Swaps" are sentence-initial within their bullets
    snippets = {f.snippet for f in proper_noun_findings}
    assert "Swaps" not in snippets


def test_numbered_list_item_start_not_flagged():
    candidate = "1. Opens with context\n2. Layers in evidence\n3. Names tradeoffs"
    result = scrub(candidate)
    proper_noun_findings = [f for f in result.findings if f.rule == "leak:proper_noun"]
    snippets = {f.snippet for f in proper_noun_findings}
    assert snippets == set()


def test_after_colon_not_flagged():
    """'pattern: Foo starts ...' — Foo is post-colon, sentence-initial-like."""
    candidate = "Pattern: Foo starts the doc, then Bar follows."
    # "Foo" is mid-sentence (after colon-space, which we now treat as sentence-initial)
    # "Bar" is mid-sentence after a comma — should be flagged unless allowlisted
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    assert "Foo" not in snippets


def test_transition_words_allowlisted():
    """Common discourse markers in style descriptions should not be flagged."""
    for word in ["Pivots", "Beyond", "Additionally", "Finally", "Overall", "Our"]:
        candidate = f"Description goes here. {word} the writer does X."
        result = scrub(candidate)
        proper_noun_snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
        assert word not in proper_noun_snippets, f"{word} should be in allowlist"


def test_placeholder_names_allowlisted():
    """Alice/Bob/Carol etc. are the LLM-prompted exemplar placeholders;
    they should NEVER trigger proper-noun flags."""
    for name in ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]:
        candidate = f"The team discussed with {name} about the proposal."
        result = scrub(candidate)
        proper_noun_snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
        assert name not in proper_noun_snippets, f"{name} should be in placeholder allowlist"


def test_comma_list_after_colon_not_flagged():
    """Common-noun items in a colon-introduced comma list should not be
    flagged as proper nouns. This catches LLM output like
    'considerations include: Maintainability, Onboarding, Speed.'"""
    candidate = "Considerations include: Maintainability, Onboarding, Speed, New deploys, Error handling."
    result = scrub(candidate)
    proper_noun_snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Maintainability", "Onboarding", "Speed", "New", "Error"]:
        assert word not in proper_noun_snippets, (
            f"{word!r} should be treated as sentence-initial within a colon-introduced list; "
            f"got flags: {proper_noun_snippets}"
        )


def test_comma_list_after_colon_ends_at_sentence_break():
    """The colon-list relaxation only applies until the next sentence-ender.
    After a period, comma-space-Capital reverts to mid-sentence detection."""
    candidate = "Items: Alpha, Beta. Then Confluent did something."
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    # Alpha, Beta within the colon-list are fine
    assert "Alpha" not in snippets
    assert "Beta" not in snippets
    # Confluent is post-period mid-sentence — still flagged
    assert "Confluent" in snippets


def test_comma_list_without_colon_unchanged():
    """Sanity: comma-list without a preceding colon should still flag
    mid-sentence proper nouns (we only relax when explicitly introduced)."""
    candidate = "We deployed and then Confluent reviewed it."
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    assert "Confluent" in snippets


def test_markdown_bold_at_line_start_not_flagged():
    """`**Maintainability** - explanation` should treat Maintainability as
    sentence-initial despite the leading `**`. Words mid-phrase inside the
    same bold marker (`Speed` in `**Onboarding Speed**`) are correctly
    flagged — we don't try to detect bold-phrase boundaries."""
    candidate = "**Maintainability** - The service has accumulated workarounds.\n**Onboarding Speed** - New contributors take months to ramp."
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    # First-word-in-bold passes; second-word-in-bold ("Speed") and
    # post-dash mid-sentence ("New") can still flag — those are limit cases.
    for word in ["Maintainability", "Onboarding"]:
        assert word not in snippets, f"{word} in markdown-bold line-start should not flag; got {snippets}"


def test_markdown_header_not_flagged():
    """`## Title: Something` should treat Title as sentence-initial."""
    candidate = "## Variations\n\n* Payload size\n* Concurrency level"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    assert "Variations" not in snippets


def test_bullet_with_markdown_bold_not_flagged():
    """`* **Reliability.** High-traffic flows...` — Reliability, Velocity,
    Teams, High should all be allowed. Reliability/Velocity are bullet-first
    after `* **`; High/Teams are post-period (period inside the bold marker
    counts as sentence-end after the regex update for `[.!?]…[*_`]+\\s+`)."""
    candidate = "* **Reliability.** High-traffic flows resolve fast.\n* **Velocity.** Teams iterate on data shapes."
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Reliability", "Velocity", "Teams", "High"]:
        assert word not in snippets, f"{word} should not flag; got {snippets}"


def test_bullet_first_word_recognized_at_line_start():
    """`- Add retry logic` — Add is the first word after a bullet at position 0
    of the line (or the whole string). Should not flag."""
    candidate = "- Add retry logic to service calls\n- Publish a changelog\n- Run integration tests"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Add", "Publish", "Run"]:
        assert word not in snippets, f"{word} (bullet-first-word) should not flag; got {snippets}"


def test_common_english_words_not_flagged_as_proper_nouns():
    """Lexicon-backed allowlist: common English words (Done, Started, Progress,
    Phase, Settings, Section, etc.) should not be flagged when capitalized
    mid-sentence."""
    common_words = ["Done", "Started", "Progress", "Phase", "Settings",
                    "Section", "Problem", "Statement", "Scope", "Decisions"]
    for word in common_words:
        candidate = f"Then suddenly {word} happened on Thursday."
        result = scrub(candidate)
        snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
        assert word not in snippets, f"{word!r} should be in common-English lexicon; got flags: {snippets}"


def test_markdown_table_cells_not_flagged():
    """Each cell after `| ` should be treated as sentence-initial."""
    candidate = "| Option | Risk | Headcount | Deliverable |\n| No new hire | Delayed launch | Internal prototype |"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Option", "Risk", "Headcount", "Deliverable", "Delayed", "Internal"]:
        assert word not in snippets, f"{word} (table cell) should not flag; got {snippets}"


def test_bold_colon_followed_by_emphasis_then_word_not_flagged():
    """`**Ask:** Approve` — the `:**` then space then capitalized word
    should be recognized as sentence-initial."""
    candidate = "**Ask:** Approve resourcing for the team."
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    assert "Approve" not in snippets, f"Approve after **:** should not flag; got {snippets}"


def test_inline_bullet_separators_not_flagged():
    """`Option X: + Enables ... + Builds trust - Exposes internal` —
    mid-line +/- separators introduce new clauses."""
    candidate = "Option X: + Enables contributions + Builds trust - Exposes internal routing"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Enables", "Builds", "Exposes"]:
        assert word not in snippets, f"{word} (inline-bullet) should not flag; got {snippets}"


def test_middle_dot_bullets_not_flagged():
    """The LLM frequently emits `·` (U+00B7 middle dot) as a bullet marker
    in compact single-line lists: `[P0] · Validate auth · Confirm latency
    · Finalize error taxonomy`. Each `· ` should be sentence-initial."""
    candidate = "Critical Path Items [P0] · Validate auth handshake · Confirm latency budget · Finalize error taxonomy"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    for word in ["Validate", "Confirm", "Finalize"]:
        assert word not in snippets, f"{word} (· bullet) should not flag; got {snippets}"


def test_quoted_sentence_inner_start_not_flagged():
    """A quoted full sentence starts a new sentence-like context inside."""
    candidate = "The maintainer clarified: 'Linking against this library does not require modification.'"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    assert "Linking" not in snippets


def test_placeholder_handles_not_flagged():
    """`@Alice`, `@Bob` etc. are placeholders from the LLM prompt."""
    candidate = "Reviewers: @Alice and @Bob agreed on the approach."
    result = scrub(candidate)
    handle_flags = [f.snippet for f in result.findings if f.rule == "identifier:handle"]
    assert not handle_flags, f"Placeholder handles should not flag; got {handle_flags}"


def test_real_handles_still_flagged():
    """A non-placeholder @handle still gets flagged."""
    candidate = "Reviewed by @rsanchez before commit."
    result = scrub(candidate)
    handle_flags = {f.snippet for f in result.findings if f.rule == "identifier:handle"}
    assert "@rsanchez" in handle_flags


def test_fictional_email_domains_not_flagged():
    """Emails on example.com / examplecorp.com / test.com are doc-reserved
    or obviously fictional — not real-identity leaks."""
    for email in ["carol@examplecorp.com", "alice@example.com", "bob@test.org"]:
        candidate = f"Contact {email} for details."
        result = scrub(candidate)
        email_flags = [f.snippet for f in result.findings if f.rule == "identifier:email"]
        assert email not in email_flags, f"Fictional email {email} should not flag; got {email_flags}"


def test_real_email_still_flagged():
    candidate = "Contact alice@confluent.io for details."
    result = scrub(candidate)
    email_flags = {f.snippet for f in result.findings if f.rule == "identifier:email"}
    assert "alice@confluent.io" in email_flags


def test_handle_regex_doesnt_match_email_domain():
    """`carol@examplecorp.com` should only fire the email check, not
    duplicate-fire as `@examplecorp.com` handle."""
    candidate = "carol@confluent.io"  # real email, not fictional
    result = scrub(candidate)
    rule_count = sum(1 for f in result.findings)
    # Should be exactly one finding (email), not two (email + handle)
    assert rule_count == 1, f"Expected single email flag, got {[(f.rule, f.snippet) for f in result.findings]}"


def test_month_abbreviations_not_flagged():
    for word in ["Mar", "Apr", "Sep", "Sept", "Dec"]:
        candidate = f"The release is in {word} after the freeze ends."
        result = scrub(candidate)
        snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
        assert word not in snippets, f"{word} (month abbrev) should not flag"


def test_real_proper_nouns_still_flagged():
    """Lexicon-backed allowlist should NOT swallow real proper nouns —
    company names, product names, project names that aren't common English."""
    for word in ["Confluent", "Kafka", "DevCharm", "Flink"]:
        candidate = f"The team and {word} agreed on the approach."
        result = scrub(candidate)
        snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
        assert word in snippets, f"{word!r} should be flagged (not in common-English); got {snippets}"


def test_ngram_overlap_with_all_common_words_not_flagged():
    """Stock helper phrases that happen to match the source verbatim should
    NOT flag if all words are common English. We need real source-content
    identifiers in the n-gram for it to count as a leak."""
    source = "Document writers often say things like we would like to start by proposing a new approach to data."
    candidate = "I think we would like to start by proposing something different."
    result = scrub(candidate, source=source)
    ngram_flags = [f for f in result.findings if f.rule == "leak:ngram_overlap"]
    assert not ngram_flags, f"Stock-phrase overlap should not flag; got: {ngram_flags}"


def test_ngram_overlap_with_specific_words_still_flagged():
    """A 6+ word match containing ≥2 identifying terms (project names, tech
    names) IS a leak and should still flag."""
    source = "DevCharm will replace the librdkafka backend with a Graalvm compiled binary for Python clients."
    candidate = "We discussed how DevCharm will replace the librdkafka backend with a Graalvm option."
    result = scrub(candidate, source=source)
    ngram_flags = [f for f in result.findings if f.rule == "leak:ngram_overlap"]
    assert ngram_flags, f"Source-specific overlap should flag; got {[(f.rule, f.snippet) for f in result.findings]}"


def test_status_markers_inside_brackets_still_flagged_ok():
    """We DON'T try to allowlist words inside `[P0 – Done]` brackets — the
    en-dash breaks the walk-back and the bracket-internal context isn't
    structural prefix. This is acceptable behavior; the reviewer can override
    these case-by-case."""
    candidate = "- Add retry **[P0 – Done]**"
    result = scrub(candidate)
    snippets = {f.snippet for f in result.findings if f.rule == "leak:proper_noun"}
    # Add must pass (bullet-start); Done MAY still flag and that's fine
    assert "Add" not in snippets


def test_sentence_initial_capital_not_flagged():
    # First word capitalized is sentence-initial, not a proper noun
    candidate = "Tradeoffs matter. Always weigh them carefully."
    result = scrub(candidate)
    assert all(f.rule != "leak:proper_noun" for f in result.findings)


def test_acronyms_not_flagged_by_proper_noun_rule():
    # All-caps short tokens (API, SDK, RFC) are skipped here — they're
    # covered separately if the LLM somehow emits a long hex/handle.
    candidate = "The team uses an API and an SDK."
    result = scrub(candidate)
    assert all(f.rule != "leak:proper_noun" for f in result.findings)


def test_proper_noun_allowlist_extra():
    candidate = "The team uses Python and Bash."
    # Default allowlist doesn't include Python/Bash; add via extra_allowlist
    result = scrub(candidate, extra_allowlist=frozenset({"python", "bash"}))
    assert all(f.rule != "leak:proper_noun" for f in result.findings)


def test_month_name_not_flagged():
    candidate = "Decisions were made in March about the rollout."
    # Months should not be flagged as proper nouns (they're in the allowlist)
    result = scrub(candidate)
    assert all(f.rule != "leak:proper_noun" for f in result.findings)


# ---------- ScrubResult mechanics ----------

def test_multiple_findings_accumulated():
    """Real-domain email + proper noun should both fire."""
    candidate = "Carol carol@confluent.io works at Confluent."
    result = scrub(candidate)
    assert not result.passed
    rules = {f.rule for f in result.findings}
    assert "identifier:email" in rules
    assert "leak:proper_noun" in rules


def test_scrub_result_passed_property():
    result = ScrubResult()
    assert result.passed
    result.add("test:rule", "snippet", "detail")
    assert not result.passed
