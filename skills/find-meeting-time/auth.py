"""Shared Google OAuth auth path for find-meeting-time helpers.

Both freebusy.py (read scope) and create_event.py (write scope) drive
the same credential lifecycle: prefer a service account if present,
otherwise use a cached InstalledAppFlow token, otherwise pop a browser
for first-time consent, otherwise fall back to ADC. Each helper passes
its own (scopes, token_file) so the tokens stay isolated per scope set
— a write token never bleeds into a script that only needs read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google.auth import default as adc_default
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

CONFIG_DIR = Path.home() / ".config" / "ai-seal-tools"
CREDENTIALS_DIR = CONFIG_DIR / "credentials"
SERVICE_ACCOUNT = CREDENTIALS_DIR / "google_service_account.json"
CLIENT_SECRETS = CREDENTIALS_DIR / "google_oauth_client.json"


def write_secret(path: Path, content: str) -> None:
    """Write content to `path` atomically with mode 0o600, even if `path`
    pre-existed with looser perms. See freebusy.py's prior docstring for
    the O_EXCL-tmp-then-rename rationale; lifted here so multiple scripts
    can share the same hardened write path for refresh tokens."""
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


def get_credentials(
    scopes: list[str],
    token_file: Path,
    *,
    impersonate: str | None = None,
    client_secrets: Path = CLIENT_SECRETS,
    service_account: Path = SERVICE_ACCOUNT,
):
    """Resolve credentials for the given scope set. Auth paths tried in
    order:
      1. Service account (with optional DWD impersonation) — preferred
         for cross-user queries when configured.
      2. Cached InstalledAppFlow token at `token_file`. Scope-aware:
         if the cached token's granted scopes don't cover `scopes`,
         it's deleted and we fall through to fresh consent.
      3. Fresh InstalledAppFlow against the OAuth Desktop client —
         opens a browser, caches the new token at `token_file`.
      4. ADC (last resort, usually blocked by Calendar's quota-project
         requirement).

    Errors out with a message pointing to SETUP.md if none of the
    above work.
    """
    if service_account.exists():
        sa = ServiceAccountCredentials.from_service_account_file(
            str(service_account), scopes=scopes
        )
        if impersonate:
            sa = sa.with_subject(impersonate)
        return sa

    if token_file.exists():
        granted = set(json.loads(token_file.read_text()).get("scopes", []))
        missing = set(scopes) - granted
        if missing:
            print(
                f"[auth] cached token at {token_file.name} is missing required scopes "
                f"({missing}); redoing consent.",
                file=sys.stderr,
            )
            token_file.unlink()
        else:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                write_secret(token_file, creds.to_json())
                return creds

    if client_secrets.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
        creds = flow.run_local_server(port=0)
        write_secret(token_file, creds.to_json())
        return creds

    adc_error: str | None = None
    try:
        creds, _ = adc_default(scopes=scopes)
        creds.refresh(Request())
        return creds
    except (DefaultCredentialsError, RefreshError) as e:
        adc_error = f"{type(e).__name__}: {e}"

    sys.exit(
        "No usable Google Calendar credentials found.\n"
        f"  Expected one of:\n"
        f"    {client_secrets}   (OAuth Desktop client — most common)\n"
        f"    {service_account}  (service account + DWD — for cross-user access)\n"
        f"  See skills/find-meeting-time/SETUP.md for step-by-step setup.\n"
        f"  ADC fallback attempt failed with: {adc_error}"
    )
