"""Tests for slack_refs_glean.parse_glean_person_to_handle and
parse_glean_messages_to_channel. The Glean MCP call lives in Claude's
hands; this module exercises only the field-mapping logic that takes
the JSON shape Glean returns and produces SlackHandle / SlackChannel."""

from __future__ import annotations

import slack_refs as sr
import slack_refs_glean as sg


# ---------------------------------------------------------------------------
# parse_glean_person_to_handle
# ---------------------------------------------------------------------------

def test_parse_handle_pairs_user_input_with_glean_email():
    """Glean's people record carries email but not the display handle —
    the user-supplied handle string gets attached as-is (lowercased)."""
    record = {
        "name": "Eve Example",
        "email": "eve@example.com",
        "title": "Principal Data Scientist",
        "datasourceToProfileLink": {
            "SLACK": "https://confluent.slack.com/team/U06CECM5P2M",
        },
    }
    h = sg.parse_glean_person_to_handle(record, handle="eve")
    assert isinstance(h, sr.SlackHandle)
    assert h.handle == "eve"
    assert h.email == "eve@example.com"
    assert h.source == "glean"


def test_parse_handle_strips_leading_at_and_lowercases():
    """User might pass '@eve' verbatim; we normalize."""
    record = {"email": "eve@example.com"}
    h = sg.parse_glean_person_to_handle(record, handle="@eve")
    assert h.handle == "eve"
    assert h.email == "eve@example.com"


def test_parse_handle_missing_email_yields_empty_string():
    """If Glean doesn't return email (rare), we get an empty email back —
    the caller decides what to do (warn / skip / use anyway)."""
    record = {"name": "Whoever"}
    h = sg.parse_glean_person_to_handle(record, handle="eve")
    assert h.email == ""


# ---------------------------------------------------------------------------
# parse_glean_messages_to_channel
# ---------------------------------------------------------------------------

def test_parse_channel_extracts_unique_author_emails():
    """Recent messages may have repeat authors; dedupe and sort."""
    messages = [
        {"author": {"email": "Alice@X"}, "text": "..."},
        {"author": {"email": "bob@x"}, "text": "..."},
        {"author": {"email": "alice@x"}, "text": "..."},  # dup, mixed case
        {"author": {"email": "carol@x"}, "text": "..."},
    ]
    c = sg.parse_glean_messages_to_channel(name="dtx-eng", message_records=messages)
    assert isinstance(c, sr.SlackChannel)
    assert c.name == "dtx-eng"
    assert c.member_emails == ("alice@x", "bob@x", "carol@x")
    assert c.source == "glean (best-effort)"
    assert "best-effort" in c.note.lower() or "inferred" in c.note.lower()


def test_parse_channel_handles_flat_author_email_shape():
    """Some Glean response shapes flatten the author to a top-level
    authorEmail field; the parser accepts either."""
    messages = [
        {"author": {"email": "alice@x"}},
        {"authorEmail": "bob@x"},  # different shape
    ]
    c = sg.parse_glean_messages_to_channel(name="ch", message_records=messages)
    assert c.member_emails == ("alice@x", "bob@x")


def test_parse_channel_skips_records_with_no_email():
    """Bot messages, missing-author records, etc. get skipped without
    crashing the parse."""
    messages = [
        {"author": {"email": "alice@x"}},
        {"author": {"name": "no-email"}},
        {"text": "no author at all"},
        {"authorEmail": ""},  # empty string
    ]
    c = sg.parse_glean_messages_to_channel(name="ch", message_records=messages)
    assert c.member_emails == ("alice@x",)


def test_parse_channel_strips_leading_hash():
    """User might pass '#dtx-eng' verbatim; we normalize."""
    c = sg.parse_glean_messages_to_channel(name="#dtx-eng", message_records=[])
    assert c.name == "dtx-eng"


def test_parse_channel_empty_messages_returns_empty_member_list():
    """No messages → no inferred members. Note still set so the cache
    entry is self-documenting."""
    c = sg.parse_glean_messages_to_channel(name="silent-ch", message_records=[])
    assert c.member_emails == ()
    assert c.note  # non-empty note documenting the limitation
