"""Tests for slack_refs.py — load/lookup/write semantics for the
@handle and #channel cache."""

from __future__ import annotations

import yaml

import slack_refs as sr


# ---------------------------------------------------------------------------
# load_handles
# ---------------------------------------------------------------------------

def test_load_handles_missing_file_empty(tmp_path):
    assert sr.load_handles(tmp_path / "no.yaml") == {}


def test_load_handles_bare_string_shorthand(tmp_path):
    """`alice: alice@x` should normalize to {'alice': {'email': 'alice@x'}}."""
    path = tmp_path / "s.yaml"
    path.write_text("handles:\n  alice: alice@x\n  bob: BOB@X\n")
    out = sr.load_handles(path)
    assert out == {"alice": {"email": "alice@x"}, "bob": {"email": "bob@x"}}


def test_load_handles_rich_record(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "handles:\n"
        "  eve:\n"
        "    email: Eve@Example.com\n"
        "    source: glean\n"
        "    fetched_at: '2026-05-15T10:00:00-07:00'\n"
    )
    out = sr.load_handles(path)
    assert out["eve"]["email"] == "eve@example.com"
    assert out["eve"]["source"] == "glean"


def test_load_handles_strips_leading_at_and_lowercases(tmp_path):
    """Keys can have leading @ or be mixed-case; both normalize."""
    path = tmp_path / "s.yaml"
    path.write_text("handles:\n  '@Alice': alice@x\n  Bob: bob@x\n")
    out = sr.load_handles(path)
    assert "alice" in out
    assert "bob" in out


def test_load_handles_skips_malformed(tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(
        "handles:\n"
        "  alice: alice@x\n"
        "  bob:\n"
        "    not_email: foo\n"  # missing email
        "  carol: [list, not, string]\n"  # wrong type
    )
    out = sr.load_handles(path)
    assert "alice" in out
    assert "bob" not in out
    assert "carol" not in out
    err = capsys.readouterr().err
    assert "malformed" in err


# ---------------------------------------------------------------------------
# load_channels
# ---------------------------------------------------------------------------

def test_load_channels_missing_file_empty(tmp_path):
    assert sr.load_channels(tmp_path / "no.yaml") == {}


def test_load_channels_normalizes_keys_and_emails(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "channels:\n"
        "  '#DTX-Eng':\n"
        "    members:\n"
        "      - Alice@X\n"
        "      - bob@x\n"
        "    source: glean\n"
    )
    out = sr.load_channels(path)
    assert "dtx-eng" in out
    assert out["dtx-eng"]["members"] == ["alice@x", "bob@x"]


def test_load_channels_skips_missing_members_field(tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(
        "channels:\n"
        "  good:\n"
        "    members: [alice@x]\n"
        "  bad:\n"
        "    source: glean\n"  # no members
    )
    out = sr.load_channels(path)
    assert "good" in out
    assert "bad" not in out
    err = capsys.readouterr().err
    assert "malformed" in err


# ---------------------------------------------------------------------------
# lookup_handle / lookup_channel / reverse_email_to_handle
# ---------------------------------------------------------------------------

def test_lookup_handle_accepts_leading_at_and_case(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("handles:\n  eve: eve@x\n")
    handles = sr.load_handles(path)
    assert sr.lookup_handle("eve", handles) == "eve@x"
    assert sr.lookup_handle("@eve", handles) == "eve@x"
    assert sr.lookup_handle("@EVE", handles) == "eve@x"
    assert sr.lookup_handle("unknown", handles) is None


def test_lookup_channel_returns_member_list(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "channels:\n"
        "  dtx-eng:\n"
        "    members: [alice@x, bob@x]\n"
    )
    channels = sr.load_channels(path)
    assert sr.lookup_channel("dtx-eng", channels) == ["alice@x", "bob@x"]
    assert sr.lookup_channel("#dtx-eng", channels) == ["alice@x", "bob@x"]
    assert sr.lookup_channel("unknown", channels) == []


def test_reverse_email_to_handle_finds_handle_by_email(tmp_path):
    """For Slack-message rendering — given an email, find what `@`-handle
    Slack should show."""
    path = tmp_path / "s.yaml"
    path.write_text("handles:\n  eve: eve@x\n  alice: alice@x\n")
    handles = sr.load_handles(path)
    assert sr.reverse_email_to_handle("eve@x", handles) == "eve"
    assert sr.reverse_email_to_handle("Eve@X", handles) == "eve"
    assert sr.reverse_email_to_handle("unknown@x", handles) is None


# ---------------------------------------------------------------------------
# write_handle / write_channel — idempotent updates
# ---------------------------------------------------------------------------

def test_write_handle_creates_file(tmp_path):
    path = tmp_path / "s.yaml"
    sr.write_handle(path, sr.SlackHandle(handle="eve", email="eve@x", source="glean"))
    handles = sr.load_handles(path)
    assert handles["eve"]["email"] == "eve@x"
    assert handles["eve"]["source"] == "glean"


def test_write_handle_replaces_existing_entry(tmp_path):
    """Re-writing the same handle overwrites the prior record."""
    path = tmp_path / "s.yaml"
    sr.write_handle(path, sr.SlackHandle(handle="alice", email="alice@x", source="manual"))
    sr.write_handle(path, sr.SlackHandle(handle="alice", email="alice@y", source="glean"))
    handles = sr.load_handles(path)
    assert handles["alice"]["email"] == "alice@y"
    assert handles["alice"]["source"] == "glean"


def test_write_handle_preserves_channels_section(tmp_path):
    """Writing a handle shouldn't blow away the channels section."""
    path = tmp_path / "s.yaml"
    sr.write_channel(path, sr.SlackChannel(name="dtx", member_emails=("alice@x",), source="manual"))
    sr.write_handle(path, sr.SlackHandle(handle="alice", email="alice@x", source="manual"))
    raw = yaml.safe_load(path.read_text())
    assert "alice" in raw["handles"]
    assert "dtx" in raw["channels"]


def test_write_channel_sorts_and_dedupes_members(tmp_path):
    """Member list normalized: lowercased, sorted, deduped."""
    path = tmp_path / "s.yaml"
    sr.write_channel(path, sr.SlackChannel(
        name="dtx",
        member_emails=("Bob@X", "alice@x", "bob@x", "alice@x"),
        source="glean",
        note="best-effort",
    ))
    channels = sr.load_channels(path)
    assert channels["dtx"]["members"] == ["alice@x", "bob@x"]
    assert channels["dtx"]["note"] == "best-effort"


def test_write_channel_omits_empty_note(tmp_path):
    """Don't write a `note: ''` field to keep the YAML clean."""
    path = tmp_path / "s.yaml"
    sr.write_channel(path, sr.SlackChannel(name="dtx", member_emails=("alice@x",), source="manual"))
    raw = yaml.safe_load(path.read_text())
    assert "note" not in raw["channels"]["dtx"]
