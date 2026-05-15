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
~/.config/ai-seal-tools/, but a *separate* token file (`google_sheets_token.json`)
so the Calendar token's narrow scope isn't disturbed.

Subcommands:
  read   --id <spreadsheet-id> --range <A1>           → JSON to stdout
  write  --id <spreadsheet-id> --range <A1> < rows.json
         rows.json is a JSON array of arrays of cell values.
  clear  --id <spreadsheet-id> --range <A1>

Examples:
  uv run utils/sheets_writer.py read --id ABC --range 'Sheet1!A1:Z'
  echo '[["a","b"],["c","d"]]' | uv run utils/sheets_writer.py write --id ABC --range 'Sheet1!A1'
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
CLIENT_SECRETS = CONFIG_DIR / "google_oauth_client.json"
TOKEN_FILE = CONFIG_DIR / "google_sheets_token.json"


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
    for name in ("read", "write", "clear"):
        sp = sub.add_parser(name)
        sp.add_argument("--id", required=True)
        sp.add_argument("--range", required=True)
    sp = sub.add_parser("meta")
    sp.add_argument("--id", required=True)
    args = p.parse_args()

    creds = get_credentials()
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    if args.cmd == "read":
        cmd_read(svc, args.id, args.range)
    elif args.cmd == "write":
        cmd_write(svc, args.id, args.range)
    elif args.cmd == "clear":
        cmd_clear(svc, args.id, args.range)
    elif args.cmd == "meta":
        cmd_meta(svc, args.id)


if __name__ == "__main__":
    main()
