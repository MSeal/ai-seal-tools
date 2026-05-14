# find-meeting-time — setup

This skill has two execution paths:

- **API path** (`freebusy.py`) — fast, structured, preferred. Requires Google
  Calendar API credentials (this doc covers the setup).
- **Browser path** (`SKILL.md`'s Playwright steps) — fallback. Works if you
  have an authenticated Google session in the Playwright MCP browser. No API
  setup needed.

The API path is what this doc gets you to.

---

## TL;DR — fresh machine, credentials already exist

You already went through the "from scratch" setup on another machine and have
the OAuth client JSON for project `mseal-devel`. To repeat on a new machine:

```bash
# 1. Clone the repo and sync deps
git clone <repo> ai-seal-tools && cd ai-seal-tools
UV_NO_CONFIG=1 uv sync

# 2. Install the skill into ~/.claude/skills/
UV_NO_CONFIG=1 uv run utils/install_skills.py

# 3. Copy the OAuth client JSON over from the old machine (or re-download it
#    from console.cloud.google.com → APIs & Services → Credentials → mseal-devel)
mkdir -p ~/.config/ai-seal-tools
scp old-machine:~/.config/ai-seal-tools/google_oauth_client.json \
    ~/.config/ai-seal-tools/google_oauth_client.json
chmod 600 ~/.config/ai-seal-tools/google_oauth_client.json

# 4. First run triggers OAuth consent in your browser; token caches afterward.
UV_NO_CONFIG=1 uv run --script skills/find-meeting-time/freebusy.py \
    --emails $(whoami)@example.com \
    --start  $(date -v+1d +%Y-%m-%dT09:00) \
    --end    $(date -v+5d +%Y-%m-%dT17:00) \
    --duration 30
```

If you see structured JSON with a `candidate_slots` array, you're done. If
not, jump to [Troubleshooting](#troubleshooting).

---

## From scratch — first time ever

Skip this section if a teammate or your past self has already set up
`mseal-devel` (or equivalent personal GCP project) and you just need the OAuth
client JSON. In that case use the [TL;DR](#tldr--fresh-machine-credentials-already-exist) above.

### Prerequisites

- A Confluent Google account (`mseal@confluent.io`)
- A GCP project you own or admin (a personal dev project is fine; need not
  involve IT for this path). If you don't have one: `console.cloud.google.com`
  → top-bar project picker → New Project. Project ID like `mseal-devel`.
- `gcloud` CLI installed and authed:
  `gcloud auth login && gcloud config set project mseal-devel`
- `uv` installed
- This repo cloned and `uv sync`'d

### Steps

#### 1. Enable the Calendar API on your project

```bash
gcloud services enable calendar-json.googleapis.com --project=mseal-devel
```

If gcloud complains about reauth, run `gcloud auth login` first.

This is the step that bit us the first time — without it, the OAuth flow
succeeds but `freebusy.query` returns 403 with a "Calendar API has not been
used in project N" message.

#### 2. Configure the OAuth consent screen

`console.cloud.google.com` → **APIs & Services → OAuth consent screen**

- **User type**: Internal (if Confluent Workspace allows it; safest), else
  External
- **App name**: anything (`find-meeting-time` is fine)
- **Support email**: your `@example.com`
- **Scopes**: add `.../auth/calendar.events.readonly` (search "events.readonly"). The narrower `calendar.freebusy` is *not* sufficient with the current helper — it doesn't surface event titles, which the movability classifier needs. If you previously set up the consent screen with `calendar.freebusy`, edit it and add `calendar.events.readonly` too; then delete `~/.config/ai-seal-tools/google_token.json` so the next run redoes consent with the new scope.
- **Test users** (if External): add your own email

If the Workspace admin policy requires app verification for external apps
requesting Calendar scopes, you'll hit "Access blocked" at first-run consent.
That's the path where you need IT involvement (see
[Alternative auth paths](#alternative-auth-paths) → service account + DWD).

#### 3. Create OAuth client credentials

Same project → **APIs & Services → Credentials → Create credentials → OAuth
client ID**

- **Application type**: Desktop app
- **Name**: anything

Hit Create, then **Download JSON**. The file looks like:

```json
{"installed":{"client_id":"...","project_id":"mseal-devel","client_secret":"GOCSPX-...","redirect_uris":["http://localhost"]}}
```

(Yes the `client_secret` is in there. Per Google's docs, Desktop client
secrets aren't actually confidential — they're embedded in shipped desktop
apps. Don't share it publicly, but it isn't a critical-leak-class secret.)

#### 4. Stage the file locally

```bash
mkdir -p ~/.config/ai-seal-tools
mv ~/Downloads/client_secret_*.json ~/.config/ai-seal-tools/google_oauth_client.json
chmod 600 ~/.config/ai-seal-tools/google_oauth_client.json
```

Filename matters — `freebusy.py` looks for exactly
`~/.config/ai-seal-tools/google_oauth_client.json`. If you name it
`google_service_account.json` instead, the helper will try to parse it as a
service-account JSON and fail confusingly.

#### 5. First run = consent flow

```bash
UV_NO_CONFIG=1 uv run --script skills/find-meeting-time/freebusy.py \
    --emails $(whoami)@example.com \
    --start  $(date -v+1d +%Y-%m-%dT09:00) \
    --end    $(date -v+3d +%Y-%m-%dT17:00) \
    --duration 30
```

Your default browser pops with Google's consent screen.

- If the consent screen says *"Google hasn't verified this app"* — that's
  expected for personal-use clients. Click **Advanced → Go to <app name>
  (unsafe)** → **Allow**.
- If it says *"Access blocked: this app's request is invalid"* or
  *"<organization> hasn't approved this app"* — Workspace admin is rejecting
  the OAuth client. See [Alternative auth paths](#alternative-auth-paths).

Once you consent, `freebusy.py` exits with a JSON dump and writes the token
to `~/.config/ai-seal-tools/google_token.json` (perms tightened to 600 on
write). All future runs reuse that token until it's revoked or invalid.

#### 6. Test with a colleague

```bash
UV_NO_CONFIG=1 uv run --script skills/find-meeting-time/freebusy.py \
    --emails someone-else@example.com \
    --start  $(date -v+1d +%Y-%m-%dT09:00) \
    --end    $(date -v+5d +%Y-%m-%dT17:00) \
    --duration 60
```

If the output's `errors` map has an entry for the colleague, their calendar
isn't shared with you at "free/busy" level. The Confluent Workspace default
is to share free/busy across the domain, so this should usually work without
the colleague taking any action.

---

## Troubleshooting

Each entry: symptom → diagnosis → fix.

### `Missing /Users/<you>/.config/ai-seal-tools/google_oauth_client.json`

- **Diagnosis**: no credentials staged.
- **Fix**: complete [step 4](#4-stage-the-file-locally) above. On a new machine
  where the JSON exists elsewhere, copy it over with the `scp` command in the
  TL;DR.

### `Google Calendar API has not been used in project <N> before or it is disabled`

- **Diagnosis**: Calendar API isn't enabled on the project that owns the
  OAuth client.
- **Fix**:
  ```bash
  gcloud services enable calendar-json.googleapis.com --project=mseal-devel
  ```
  Wait ~30 seconds for propagation, then re-run. (Replace `mseal-devel` with
  the `project_id` in your `google_oauth_client.json` if different.)

### `Access blocked: <app name> has not completed the Google verification process`

- **Diagnosis**: External OAuth consent screen, app is unverified. Personal
  workaround on the consent screen is Advanced → Go to <app> (unsafe).
- **Fix**: complete consent via the unsafe path. To remove the warning
  permanently you'd need to verify the app with Google — overkill for
  personal use.

### `Access blocked: Confluent hasn't approved this app` / `<org> hasn't approved`

- **Diagnosis**: Workspace admin policy is rejecting the OAuth client.
- **Fix**: this OAuth-Desktop-client path doesn't work without IT
  intervention. Switch to the service-account + DWD path in
  [Alternative auth paths](#alternative-auth-paths), which has a separate IT
  ask.

### `RefreshError: invalid_grant`

- **Diagnosis**: cached token is no longer valid. Either revoked, too old,
  or the OAuth client got rotated.
- **Fix**:
  ```bash
  rm ~/.config/ai-seal-tools/google_token.json
  ```
  Re-run the helper. The script falls through to the InstalledAppFlow path
  and opens a fresh browser consent.

### `RefreshError: invalid_scope: Bad Request`

- **Diagnosis**: the cached token was issued with a narrower scope than the
  helper now requests (e.g., the helper was upgraded from
  `calendar.freebusy` to `calendar.events.readonly`). The helper's
  scope-mismatch detector should catch this automatically and unlink the
  token, but if you see this error directly:
- **Fix**:
  1. Ensure the new scope is added to the OAuth consent screen in
     `console.cloud.google.com` (APIs & Services → OAuth consent screen →
     Edit App → Scopes → add `.../auth/calendar.events.readonly`).
  2. `rm ~/.config/ai-seal-tools/google_token.json`
  3. Re-run the helper. Browser pops with the new scope, you re-consent.

### `your application is authenticating by using local Application Default Credentials. The calendar-json.googleapis.com API requires a quota project`

- **Diagnosis**: the helper fell through to the ADC path. This used to happen
  when no OAuth client was configured; with the current code, this only
  appears if `google_oauth_client.json` is missing and ADC creds exist.
- **Fix**: stage the OAuth client JSON properly (see step 4). ADC is the
  fallback of last resort and runs into Google's quota-project requirement
  for Workspace APIs — not worth fighting.

### `HttpError 403 ... reason: 'notFound'` for a specific attendee

- **Diagnosis**: the target user's calendar isn't visible to you at
  free/busy level (and they may not exist in the directory).
- **Fix**: confirm the email is correct. If it is, ask the user to share
  their calendar with you at "see only free/busy" level
  (calendar.google.com → Settings → Settings for my calendar → Share with
  specific people).

### Helper hangs at "Please visit this URL to authorize this application"

- **Diagnosis**: `webbrowser.open()` didn't auto-launch (rare on macOS,
  occasional on headless / SSH sessions), so the script is waiting for you to
  open the URL manually.
- **Fix**: paste the URL into a browser yourself. Complete consent. The
  script will pick up the redirect on localhost and proceed.

### "I deleted everything, where do I start?"

Order to rebuild on a clean machine, from worst-case to best:

1. **Lost both the JSON and the GCP project**: full restart from
   [From scratch](#from-scratch--first-time-ever).
2. **Lost the JSON, project still exists**: redownload the OAuth client JSON
   from `console.cloud.google.com` → APIs & Services → Credentials → click
   the existing client → Download JSON. Save to `~/.config/ai-seal-tools/`.
3. **Lost only the token**: just re-run the helper; it'll consent again and
   re-cache.
4. **Lost only the cached deps**: `UV_NO_CONFIG=1 uv sync`. (The
   `freebusy.py` script uses inline PEP 723 deps, so a separate `uv sync`
   isn't strictly required, but it doesn't hurt.)

---

## Alternative auth paths

### Service account + Domain-Wide Delegation (DWD)

The "calendar bot" pattern. Requires Workspace admin to enable DWD for a
service account in a Confluent-owned project. Pros: works against *any* user
in the directory without per-user consent or sharing. Cons: needs IT.

To request from IT:

> Please provision a service account in a Confluent-owned GCP project for use
> with the Google Calendar API:
>
> - **Project**: an existing project I have access to, or a new sandbox.
> - **API enabled on project**: `calendar-json.googleapis.com`
> - **Service account**: name `mseal-calendar-finder`, purpose: read
>   free/busy of `@example.com` users for personal scheduling tooling.
> - **Domain-wide delegation**: enabled on this service account. In Workspace
>   Admin Console → Security → API Controls → Domain-wide Delegation, add
>   the service account's **Client ID** with scope:
>   `https://www.googleapis.com/auth/calendar.freebusy`
> - **Deliverable**: JSON key file for the service account.

Then save it as:

```bash
mv <downloaded-sa-key>.json ~/.config/ai-seal-tools/google_service_account.json
chmod 600 ~/.config/ai-seal-tools/google_service_account.json
```

The helper auto-detects this file and uses it before the OAuth client path.
Pass `--impersonate <your-email>` when invoking, so the SA acts as you.

### gcloud ADC (don't bother for Calendar)

`gcloud auth application-default login --scopes=...calendar.freebusy` does
grant the scope cleanly, but Workspace APIs called via user-cred ADC require
`x-goog-user-project` to be a project where you have
`serviceusage.services.use`. On managed Confluent projects you usually lack
this; on personal projects you have it but the OAuth client path is simpler
anyway.

This path exists in the helper as a last-resort fallback, but in practice
you won't use it for Calendar work.

---

## What's a credential, anyway

- **API key**: identifies the *project* for quota/billing. Cannot access
  private data like a colleague's calendar. Not useful for this skill.
- **OAuth client JSON** (`google_oauth_client.json`): identifies *an app*.
  Combined with user consent (the InstalledAppFlow), grants access to that
  user's calendar.
- **OAuth token JSON** (`google_token.json`): the granted access + refresh
  token for a specific user against a specific OAuth client. Cached after
  first consent. Sensitive — keep at 600.
- **Service account JSON** (`google_service_account.json`): identifies a
  bot. With DWD, can act as any user in the org. Sensitive — keep at 600
  and rotate immediately if leaked.

When someone says "we'll give you the API key," confirm which of the above
they mean. Those four solve different problems.

---

## Security checklist

- All credential files under `~/.config/ai-seal-tools/` should be `chmod 600`.
- The repo's `.gitignore` has a safety net for `google_service_account.json`,
  `google_oauth_client.json`, and `google_token.json` so they can't land in
  git even if you copy them into the repo dir by mistake.
- The `calendar.freebusy` scope is read-only and returns only busy time
  ranges (no titles, attendees, locations). Smallest blast radius for the
  use case.
- If you ever broaden to `events.readonly` (to see titles for richer
  ranking), that's a separate, larger access grant — re-run the consent flow
  and reconsider whether DWD is the right pattern at that point.
