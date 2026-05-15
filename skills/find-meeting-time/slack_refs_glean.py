"""Glean-flavored parsers for the slack_refs contract.

The Glean MCP isn't directly callable from Python — Claude makes the
search/chat calls inside the skill flow and then writes resolved
references through record_slack_ref.py. This module owns the field
mapping from Glean's shapes; if/when a real Slack MCP is approved,
add a sibling `slack_refs_slack.py` exporting the same parse_* signatures
and swap which module the skill calls. The contract (SlackHandle,
SlackChannel) and the CLI/cache stay identical.

For @handle resolution (Glean `app=people`):
    The people-record shape from Glean has `email` plus
    `datasourceToProfileLink.SLACK` like
    `https://confluent.slack.com/team/U06CECM5P2M`. Glean doesn't
    expose the display handle directly, so we accept the handle as
    an input parameter (the string the user typed after `@`) and
    pair it with the resolved email.

For #channel member resolution (Glean `app=slack`, channel filter):
    Glean indexes message authors but isn't an authoritative
    channel-membership API. We extract unique author emails from
    a list of recent messages; the resulting SlackChannel carries
    a `note` field documenting the inference.
"""

from __future__ import annotations

from slack_refs import SlackChannel, SlackHandle


def parse_glean_person_to_handle(record: dict, handle: str) -> SlackHandle:
    """Pair the user-supplied @handle with the email from a Glean
    people record. Glean returns email but not the human-readable
    Slack display handle, so we keep `handle` as an explicit input."""
    email = (record.get("email") or "").strip().lower()
    return SlackHandle(handle=handle.lstrip("@").lower(), email=email, source="glean")


def parse_glean_messages_to_channel(
    name: str,
    message_records: list[dict],
) -> SlackChannel:
    """Extract unique author emails from a list of Glean Slack-message
    search results. Each record is expected to have an `author.email`
    (or similar) field. Missing emails are skipped silently — the
    note field flags that the result is best-effort."""
    members: set[str] = set()
    for record in message_records:
        author = record.get("author") or {}
        email = (author.get("email") or "").strip().lower()
        if email:
            members.add(email)
        # Some Glean shapes flatten author info to top-level:
        elif (top_email := (record.get("authorEmail") or "")).strip():
            members.add(top_email.strip().lower())
    return SlackChannel(
        name=name.lstrip("#").lower(),
        member_emails=tuple(sorted(members)),
        source="glean (best-effort)",
        note=(
            "Members inferred from recent message authors in Glean's "
            "Slack index. Lurkers and recent joiners may be missing. "
            "Re-fetch periodically or hand-edit to add."
        ),
    )
