#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "google-api-python-client>=2.196",
#   "google-auth-oauthlib>=1.4",
# ]
# ///
"""sheets_writer.py — read/write Google Sheets reusing the ai-seal-tools OAuth client.

Auth model mirrors freebusy.py: same `google_oauth_client.json` at
~/.config/ai-seal-tools/credentials/, but a *separate* token file
(`google_sheets_token.json`) so the Calendar token's narrow scope
isn't disturbed.

Subcommands:
  read   --id <sid> --range <A1>                     → JSON to stdout
  write  --id <sid> --range <A1> < rows.json         → 2D block, range-anchored
         rows.json is a JSON array of arrays of cell values. The range
         can target a single cell (A1), one row (A2:F2), one column
         (D2:D7), or a block — `write` is the workhorse for all three.
  cell   --id <sid> --range <A1> --value <v>         → single-cell convenience
         No stdin; --value is a scalar string. Sheets parses it the
         same way it parses user input (USER_ENTERED).
  batch  --id <sid> < updates.json                   → atomic multi-range update
         updates.json is `[{"range": "...", "values": [[...]]}, ...]`,
         one round-trip, all-or-nothing semantics from the API side.
  append --id <sid> --range <table-range> < rows.json → insert rows below
         Appends to the first table found in `range`; new rows shift
         existing data down if needed (insertDataOption=INSERT_ROWS).
  clear  --id <sid> --range <A1>
  meta   --id <sid>                                  → tabs, dimensions

Examples:
  # Read
  uv run utils/sheets_writer.py read  --id ABC --range 'Sheet1!A1:Z'
  # Single cell
  uv run utils/sheets_writer.py cell  --id ABC --range 'Sheet1!B3' --value 'Terminal'
  # One column
  echo '[["x"],["y"],["z"]]' | uv run utils/sheets_writer.py write --id ABC --range 'Sheet1!D2:D4'
  # One row
  echo '[["a","b","c"]]'      | uv run utils/sheets_writer.py write --id ABC --range 'Sheet1!A5:C5'
  # Non-contiguous in one call
  echo '[{"range":"Sheet1!A3","values":[["Terminal"]]},
         {"range":"Sheet1!D5","values":[["new"]]}]' | \\
      uv run utils/sheets_writer.py batch --id ABC

Prefer targeted ranges over rewriting the whole sheet — bulk `write`
should be reserved for changes that affect most of the document.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google.auth import default as adc_default
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Sheets requires either the spreadsheets scope or drive (drive grants both).
# We accept either — ADC tokens are usually `cloud-platform` + `drive`.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ADC_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CONFIG_DIR = Path.home() / ".config" / "ai-seal-tools"
CREDENTIALS_DIR = CONFIG_DIR / "credentials"
CLIENT_SECRETS = CREDENTIALS_DIR / "google_oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "google_sheets_token.json"


def _write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    tmp.replace(path)


def get_credentials() -> Credentials:
    # 1) Application Default Credentials — uses gcloud's project (Sheets API
    #    enabled there by default) and avoids the per-OAuth-client API enable
    #    dance. The check-gcp-auth.sh hook keeps this token fresh with Drive
    #    scope, which is sufficient for the Sheets API.
    #    User ADC creds need an explicit quota project, otherwise the API
    #    bills the OAuth client's own project — which usually doesn't have
    #    Sheets enabled. Pull it from gcloud's active config.
    quota_project = os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT")
    if not quota_project:
        try:
            import subprocess
            out = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                check=True, capture_output=True, text=True,
            )
            quota_project = out.stdout.strip() or None
        except Exception:
            quota_project = None
    try:
        creds, _ = adc_default(scopes=ADC_SCOPES, quota_project_id=quota_project)
        creds.refresh(Request())
        return creds
    except (DefaultCredentialsError, RefreshError):
        pass

    # 2) Cached OAuth-client token (Sheets-scoped).
    if TOKEN_FILE.exists():
        granted = set(json.loads(TOKEN_FILE.read_text()).get("scopes", []))
        if not (set(SCOPES) - granted):
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _write_secret(TOKEN_FILE, creds.to_json())
                return creds
        else:
            TOKEN_FILE.unlink()
    if not CLIENT_SECRETS.exists():
        sys.exit(f"Missing OAuth client at {CLIENT_SECRETS}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
    creds = flow.run_local_server(port=0)
    _write_secret(TOKEN_FILE, creds.to_json())
    return creds


def cmd_read(svc, sid: str, rng: str) -> None:
    result = svc.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_write(svc, sid: str, rng: str) -> None:
    rows = json.load(sys.stdin)
    result = svc.spreadsheets().values().update(
        spreadsheetId=sid,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_cell(svc, sid: str, rng: str, value: str) -> None:
    """Single-cell update. Same API call as cmd_write, just easier to type."""
    result = svc.spreadsheets().values().update(
        spreadsheetId=sid,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_batch(svc, sid: str) -> None:
    """Atomic multi-range update. Stdin is a JSON array of {range, values}.

    One API round-trip, all-or-nothing — better than N serial `write` calls
    when touching several non-contiguous regions (e.g. fixing two cells in
    different columns, or rewriting a column header + a footer row).
    """
    data = json.load(sys.stdin)
    if not isinstance(data, list) or not all(
        isinstance(d, dict) and "range" in d and "values" in d for d in data
    ):
        sys.exit("batch input must be a JSON array of {range, values} objects")
    result = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_append(svc, sid: str, rng: str) -> None:
    """Append rows to the table at `rng`. Stdin is a JSON array-of-arrays."""
    rows = json.load(sys.stdin)
    result = svc.spreadsheets().values().append(
        spreadsheetId=sid,
        range=rng,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_clear(svc, sid: str, rng: str) -> None:
    result = svc.spreadsheets().values().clear(
        spreadsheetId=sid, range=rng, body={}
    ).execute()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_meta(svc, sid: str) -> None:
    result = svc.spreadsheets().get(spreadsheetId=sid).execute()
    # Strip noisy fields, keep what we usually want
    out = {
        "title": result.get("properties", {}).get("title"),
        "spreadsheetId": result.get("spreadsheetId"),
        "sheets": [
            {
                "title": s["properties"]["title"],
                "sheetId": s["properties"]["sheetId"],
                "gridProperties": s["properties"].get("gridProperties"),
            }
            for s in result.get("sheets", [])
        ],
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    # Range-anchored commands: read/write/clear/append all need --id + --range
    for name in ("read", "write", "clear", "append"):
        sp = sub.add_parser(name)
        sp.add_argument("--id", required=True)
        sp.add_argument("--range", required=True)
    # cell needs an explicit --value
    sp = sub.add_parser("cell")
    sp.add_argument("--id", required=True)
    sp.add_argument("--range", required=True)
    sp.add_argument("--value", required=True)
    # batch and meta need only --id (batch reads from stdin)
    for name in ("batch", "meta"):
        sp = sub.add_parser(name)
        sp.add_argument("--id", required=True)
    args = p.parse_args()

    creds = get_credentials()
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    if args.cmd == "read":
        cmd_read(svc, args.id, args.range)
    elif args.cmd == "write":
        cmd_write(svc, args.id, args.range)
    elif args.cmd == "cell":
        cmd_cell(svc, args.id, args.range, args.value)
    elif args.cmd == "batch":
        cmd_batch(svc, args.id)
    elif args.cmd == "append":
        cmd_append(svc, args.id, args.range)
    elif args.cmd == "clear":
        cmd_clear(svc, args.id, args.range)
    elif args.cmd == "meta":
        cmd_meta(svc, args.id)


if __name__ == "__main__":
    main()
