"""Tests for utils/sheets_writer.py — auth resolution, secret file perms,
and command dispatch against a mock Sheets service.

We don't exercise live OAuth or HTTP; we monkeypatch the module's auth
entry points and feed a hand-rolled fake Google API client. The goal is
to lock down the bits we'll actually regress on:
  - 0o600 perms on secret writes (atomic replace through tmp inode)
  - Scope-mismatch handling on cached tokens
  - Stdin JSON → values().update() wiring
"""

from __future__ import annotations

import io
import json
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

import sheets_writer as sw
from google.auth.exceptions import DefaultCredentialsError


# ---------------------------------------------------------------------------
# _write_secret
# ---------------------------------------------------------------------------

def test_write_secret_creates_file_with_0600(tmp_path):
    target = tmp_path / "secret.json"
    sw._write_secret(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_write_secret_overwrites_existing_with_0600(tmp_path):
    """Pre-existing file at 0o644 must end up 0o600 after rewrite (the whole
    reason _write_secret uses a fresh tmp inode + atomic replace)."""
    target = tmp_path / "secret.json"
    target.write_text("old")
    target.chmod(0o644)
    sw._write_secret(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_secret_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "dir" / "secret.json"
    sw._write_secret(target, "x")
    assert target.read_text() == "x"


def test_write_secret_cleans_up_stale_tmp(tmp_path):
    """A leftover .tmp file from a prior crashed run shouldn't block the
    next write — _write_secret unlinks it before O_EXCL'ing the new inode."""
    target = tmp_path / "secret.json"
    stale_tmp = target.with_suffix(target.suffix + ".tmp")
    stale_tmp.write_text("stale")
    sw._write_secret(target, "fresh")
    assert target.read_text() == "fresh"
    assert not stale_tmp.exists()


# ---------------------------------------------------------------------------
# get_credentials — auth resolution
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    """Point sheets_writer at a tmp config dir and force ADC to fail, so
    tests can drive the OAuth-token / client-secrets paths deterministically.
    """
    token = tmp_path / "google_sheets_token.json"
    client = tmp_path / "google_oauth_client.json"
    monkeypatch.setattr(sw, "TOKEN_FILE", token)
    monkeypatch.setattr(sw, "CLIENT_SECRETS", client)
    monkeypatch.setattr(sw, "CONFIG_DIR", tmp_path)

    def adc_fails(*a, **kw):
        raise DefaultCredentialsError("no ADC in tests")
    monkeypatch.setattr(sw, "adc_default", adc_fails)
    return token, client


def _fake_creds(valid=True, expired=False, refresh_token="rt"):
    c = mock.MagicMock()
    c.valid = valid
    c.expired = expired
    c.refresh_token = refresh_token
    c.to_json.return_value = '{"refresh_token": "rt"}'
    return c


def test_get_credentials_returns_cached_token_when_scopes_match(isolated_auth, monkeypatch):
    token, _ = isolated_auth
    token.write_text(json.dumps({"scopes": sw.SCOPES, "refresh_token": "x"}))
    creds = _fake_creds(valid=True)
    monkeypatch.setattr(
        sw.Credentials, "from_authorized_user_file",
        lambda path, scopes: creds,
    )
    assert sw.get_credentials() is creds


def test_get_credentials_refreshes_expired_token(isolated_auth, monkeypatch):
    token, _ = isolated_auth
    token.write_text(json.dumps({"scopes": sw.SCOPES, "refresh_token": "x"}))
    creds = _fake_creds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(
        sw.Credentials, "from_authorized_user_file",
        lambda path, scopes: creds,
    )
    result = sw.get_credentials()
    assert result is creds
    creds.refresh.assert_called_once()
    # refreshed token gets re-persisted with 0o600
    assert token.read_text() == '{"refresh_token": "rt"}'
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_get_credentials_unlinks_token_when_scopes_missing(isolated_auth, monkeypatch):
    """Cached token doesn't grant the required scopes → delete it and fall
    through to the OAuth flow rather than handing back narrow creds."""
    token, client = isolated_auth
    token.write_text(json.dumps({"scopes": ["https://example.com/wrong"]}))
    client.write_text("{}")  # presence is what matters; flow is mocked
    fresh = _fake_creds()
    flow = mock.MagicMock()
    flow.run_local_server.return_value = fresh
    monkeypatch.setattr(
        sw.InstalledAppFlow, "from_client_secrets_file",
        lambda path, scopes: flow,
    )
    result = sw.get_credentials()
    assert result is fresh
    # OAuth flow was invoked because the cached token was deleted
    flow.run_local_server.assert_called_once()
    # And the new token landed on disk
    assert token.exists()


def test_get_credentials_exits_when_no_token_and_no_client_secrets(isolated_auth):
    with pytest.raises(SystemExit) as exc:
        sw.get_credentials()
    assert "Missing OAuth client" in str(exc.value)


def test_get_credentials_prefers_adc_when_available(tmp_path, monkeypatch):
    """When ADC works, the cached/OAuth paths are never touched."""
    monkeypatch.setattr(sw, "TOKEN_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(sw, "CLIENT_SECRETS", tmp_path / "nope_client.json")
    adc_creds = _fake_creds()
    monkeypatch.setattr(sw, "adc_default", lambda *a, **kw: (adc_creds, "proj"))
    assert sw.get_credentials() is adc_creds
    adc_creds.refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Command dispatch — verify the right Sheets API method is called with the
# right args. Uses a fake service object so we don't hit the network.
# ---------------------------------------------------------------------------

class _FakeValuesAPI:
    def __init__(self):
        self.get_calls = []
        self.update_calls = []
        self.clear_calls = []
        self.batch_update_calls = []
        self.append_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _FakeExecutor({"range": kwargs["range"], "values": [["a", "b"]]})

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return _FakeExecutor({"updatedCells": 4, "updatedRange": kwargs["range"]})

    def clear(self, **kwargs):
        self.clear_calls.append(kwargs)
        return _FakeExecutor({"clearedRange": kwargs["range"]})

    def batchUpdate(self, **kwargs):
        self.batch_update_calls.append(kwargs)
        n = sum(
            len(d["values"]) * (len(d["values"][0]) if d["values"] else 0)
            for d in kwargs["body"]["data"]
        )
        return _FakeExecutor({"totalUpdatedCells": n, "responses": kwargs["body"]["data"]})

    def append(self, **kwargs):
        self.append_calls.append(kwargs)
        return _FakeExecutor({
            "updates": {
                "updatedRange": kwargs["range"],
                "updatedRows": len(kwargs["body"]["values"]),
            }
        })


class _FakeExecutor:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeSpreadsheets:
    def __init__(self):
        self._values = _FakeValuesAPI()
        self.get_calls = []

    def values(self):
        return self._values

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _FakeExecutor({
            "properties": {"title": "T"},
            "spreadsheetId": kwargs["spreadsheetId"],
            "sheets": [{"properties": {"title": "Sheet1", "sheetId": 0, "gridProperties": {"rowCount": 10, "columnCount": 5}}}],
        })


class _FakeService:
    def __init__(self):
        self._sheets = _FakeSpreadsheets()

    def spreadsheets(self):
        return self._sheets


def test_cmd_read_invokes_values_get(capsys):
    svc = _FakeService()
    sw.cmd_read(svc, "sid-123", "Sheet1!A1:B")
    out = json.loads(capsys.readouterr().out)
    assert out["range"] == "Sheet1!A1:B"
    assert svc.spreadsheets()._values.get_calls == [
        {"spreadsheetId": "sid-123", "range": "Sheet1!A1:B"}
    ]


def test_cmd_write_reads_stdin_and_updates(monkeypatch, capsys):
    svc = _FakeService()
    rows = [["a", "b"], ["c", "d"]]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(rows)))
    sw.cmd_write(svc, "sid-123", "Sheet1!A1")
    out = json.loads(capsys.readouterr().out)
    assert out["updatedCells"] == 4
    [call] = svc.spreadsheets()._values.update_calls
    assert call["spreadsheetId"] == "sid-123"
    assert call["range"] == "Sheet1!A1"
    assert call["valueInputOption"] == "USER_ENTERED"
    assert call["body"] == {"values": rows}


def test_cmd_clear_invokes_values_clear(capsys):
    svc = _FakeService()
    sw.cmd_clear(svc, "sid-123", "Sheet1!A1:Z")
    out = json.loads(capsys.readouterr().out)
    assert out["clearedRange"] == "Sheet1!A1:Z"
    [call] = svc.spreadsheets()._values.clear_calls
    assert call == {"spreadsheetId": "sid-123", "range": "Sheet1!A1:Z", "body": {}}


def test_cmd_cell_wraps_scalar_into_2d_block(capsys):
    """cmd_cell is a thin shim that calls values().update() with [[value]]."""
    svc = _FakeService()
    sw.cmd_cell(svc, "sid-123", "Sheet1!B3", "Terminal")
    out = json.loads(capsys.readouterr().out)
    assert out["updatedRange"] == "Sheet1!B3"
    [call] = svc.spreadsheets()._values.update_calls
    assert call["spreadsheetId"] == "sid-123"
    assert call["range"] == "Sheet1!B3"
    assert call["valueInputOption"] == "USER_ENTERED"
    assert call["body"] == {"values": [["Terminal"]]}


def test_cmd_batch_forwards_array_to_batch_update(monkeypatch, capsys):
    """batch payload reaches values().batchUpdate() unchanged, with
    USER_ENTERED set by us (not the caller)."""
    svc = _FakeService()
    updates = [
        {"range": "Sheet1!A3", "values": [["Terminal"]]},
        {"range": "Sheet1!D5:D7", "values": [["a"], ["b"], ["c"]]},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(updates)))
    sw.cmd_batch(svc, "sid-123")
    out = json.loads(capsys.readouterr().out)
    # 1 + 3 = 4 cells touched, fake API echoes them
    assert out["totalUpdatedCells"] == 4
    [call] = svc.spreadsheets()._values.batch_update_calls
    assert call["spreadsheetId"] == "sid-123"
    assert call["body"]["valueInputOption"] == "USER_ENTERED"
    assert call["body"]["data"] == updates


def test_cmd_batch_rejects_non_array_input(monkeypatch):
    """Anything other than `[{range, values}, ...]` is a config error, not a
    silently-misshapen API call. Exit early with a clear message."""
    svc = _FakeService()
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"range": "A1", "values": [["x"]]}'))
    with pytest.raises(SystemExit) as exc:
        sw.cmd_batch(svc, "sid-123")
    assert "batch input must be a JSON array" in str(exc.value)
    # And nothing was sent to the API
    assert svc.spreadsheets()._values.batch_update_calls == []


def test_cmd_batch_rejects_array_with_malformed_entries(monkeypatch):
    svc = _FakeService()
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO('[{"range": "A1"}]'),  # missing 'values'
    )
    with pytest.raises(SystemExit):
        sw.cmd_batch(svc, "sid-123")
    assert svc.spreadsheets()._values.batch_update_calls == []


def test_cmd_append_uses_insert_rows_option(monkeypatch, capsys):
    """append() must set insertDataOption=INSERT_ROWS so existing rows below
    the table aren't overwritten — Sheets' default OVERWRITE has bitten us."""
    svc = _FakeService()
    rows = [["a", "b"], ["c", "d"]]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(rows)))
    sw.cmd_append(svc, "sid-123", "Sheet1!A:F")
    out = json.loads(capsys.readouterr().out)
    assert out["updates"]["updatedRows"] == 2
    [call] = svc.spreadsheets()._values.append_calls
    assert call["spreadsheetId"] == "sid-123"
    assert call["range"] == "Sheet1!A:F"
    assert call["valueInputOption"] == "USER_ENTERED"
    assert call["insertDataOption"] == "INSERT_ROWS"
    assert call["body"] == {"values": rows}


def test_cmd_meta_summarizes_spreadsheet(capsys):
    svc = _FakeService()
    sw.cmd_meta(svc, "sid-123")
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "title": "T",
        "spreadsheetId": "sid-123",
        "sheets": [{
            "title": "Sheet1",
            "sheetId": 0,
            "gridProperties": {"rowCount": 10, "columnCount": 5},
        }],
    }
