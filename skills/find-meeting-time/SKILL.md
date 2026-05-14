---
name: find-meeting-time
description: Find the best time to meet with a set of company colleagues by inspecting Google Calendar availability. Handles the easy case (everyone free) and the common hard case (no obvious slot — rank candidates by how movable the conflicts look). Has two execution paths: a fast API path via freebusy.py (preferred, requires service-account creds — see SETUP.md), and a browser fallback via Playwright MCP that scrapes the "Find a time" view. Argument is a free-form description of the meeting (attendees, duration, date range).
---

# Find Meeting Time

Answer "when can we meet?" — and, when nothing is fully free, "what's the least bad time?"

## Execution paths

**Prefer the API path** when `~/.config/ai-seal-tools/google_service_account.json` (or other Google creds — see `SETUP.md`) is present. It's faster, structured, and doesn't depend on an interactive browser session being signed in.

```bash
UV_NO_CONFIG=1 uv run --script "$(dirname "$0")/freebusy.py" \
  --emails <comma-separated-emails> \
  --start  <ISO 8601> \
  --end    <ISO 8601> \
  --duration <minutes> \
  --impersonate mseal@confluent.io
```

The helper outputs JSON with `busy` ranges per attendee, plus `candidate_slots` already sorted by conflict count. Then Claude does the ranking refinement (time-of-day quality, lunch avoidance, etc.) and writes the user-facing summary.

**Fall back to the browser path** when no credentials are configured (the helper exits with a setup message listing the options). The browser path follows the snapshot-driven / vision-fallback discipline in `prompts/browsing.md`.

## Inputs

`$ARGUMENTS` — a free-form description of the meeting. Examples:

- `30 min with alice@example.com and bob@example.com this week`
- `1 hr with the platform team Tue or Wed afternoon`
- `45 min sync with Carol, Dave, and Eve before Friday`

Resolve the inputs into a concrete plan:
- **Attendees**: emails preferred; names are OK if Google Calendar's autocomplete will find them in the Confluent directory.
- **Duration**: default 30 min if not specified.
- **Date range**: convert relative phrases ("this week", "next Tue") to absolute dates using today's date from the system context. Default to the next 5 business days if unspecified.
- **Working hours**: default to 9:00–17:00 in the user's local timezone. If multiple timezones are at play, prefer overlap windows and call out the timezone math in the final answer.

If the description is too ambiguous to act on (no attendees, or a window that's clearly nonsensical), ask **one** clarifying question, then proceed.

## Steps

1. `browser_navigate("https://calendar.google.com")`. If redirected to a sign-in page, stop and tell the user — they need to sign in to their Confluent Google account in the browser session. Do not attempt to type credentials.
2. `browser_snapshot()`. Confirm you're on the calendar view (look for the main grid, the "Create" button, and the user's account chip in the top-right).
3. Click **Create** → **Event** (or press `c` as a shortcut). When the quick-create popover appears, click **More options** to open the full event editor — the side-by-side scheduling view lives there.
4. In the full editor:
   - Title: `[DRAFT — do not save]` (we'll discard this draft at the end)
   - Set the duration to the requested length on any date inside the target range. The starting date doesn't matter — we'll move the slot around in the find-a-time view.
   - Add each attendee in the **Guests** field. Wait for autocomplete to resolve them to a directory entry before pressing Enter. If a name doesn't resolve, surface that to the user and either skip them or stop, depending on how critical the user said they are.
5. Switch to the **Find a time** tab (sometimes labeled "Schedule" or shown as a calendar icon next to "Guests" — depends on the UI version). This opens the side-by-side grid with one row per attendee.
6. Navigate the grid through the requested date range:
   - Use the date arrows or the mini-calendar to step day-by-day or week-by-week.
   - For each working-hours window, `browser_take_screenshot()` of the visible grid. The snapshot tree usually doesn't capture conflict block titles cleanly, so vision is the right tool here.
   - For each candidate slot (every 15- or 30-min offset that fits the requested duration inside working hours), note: who has a conflict, and any visible conflict title.
7. **Rank the candidate slots.** Use this priority order:
   1. **All-free slots** — surface immediately, no further ranking needed beyond time-of-day quality.
   2. **One-conflict slots, conflict appears movable** — recurring 1:1s, "focus time", "block", "hold", or generic titles like "busy" / "tentative" all suggest the attendee can probably shift it.
   3. **One-conflict slots, conflict looks fixed** — external customer meetings, all-hands, exec syncs, interviews, OOO. Lower priority.
   4. **Multi-conflict slots** — only surface if nothing better exists. Rank by total conflict severity (fewer + more-movable beats more + fixed).
   - Tie-breakers: avoid lunch (12:00–13:00 local), avoid first/last 30 min of the working day, prefer earlier in the date range, prefer same-day-of-week consistency if the request implies a recurring slot.
   - If an attendee's calendar only shares free/busy (no titles visible), treat conflicts as "unknown — assume fixed" and note it in the output.
8. Close the draft without saving:
   - Click the X or press Esc.
   - If Calendar prompts "Discard event?" or "Save changes?", click **Discard**. Do not click Save — we never want a phantom "[DRAFT]" event landing on real calendars.
   - `browser_snapshot()` to confirm you're back on the main calendar view.
9. `browser_close()`.

## Output format

Reply to the user with:

```
Meeting: <duration> with <attendees> between <start date> and <end date> (<timezone>)

Recommendations:

1. <Day, Date> <start>–<end> — **All free**
   • No conflicts.

2. <Day, Date> <start>–<end> — 1 conflict (likely movable)
   • Alice: "Weekly 1:1 with manager" (recurring, easily shifted)
   • Trade-off: 30 min before Alice's team standup.

3. <Day, Date> <start>–<end> — 2 conflicts
   • Bob: free/busy only — title unknown, treat as fixed
   • Carol: "Focus block" (movable)

Notes:
- <anything notable — timezone math, attendees not found in directory, calendars not shared, etc.>
```

Aim for 3–5 ranked recommendations. If there's a clear all-free winner, you can return just that one with a note that other slots were considered.

## Failure modes to watch for

- **Not signed in**: the page redirects to `accounts.google.com`. Tell the user; do not type credentials.
- **Attendee not in directory**: autocomplete shows no match. Skip them with a note, or ask the user if they're critical.
- **Free/busy only (no titles visible)**: Calendar shows colored blocks with no text. Note this in the output and rank those slots conservatively — we can't tell if conflicts are movable.
- **"Find a time" tab missing**: on narrow windows or older UI variants it can be hidden behind a menu. Try widening the browser (`browser_resize`) or switching to the "Day" view with guests added — same data, different layout.
- **Draft accidentally saved**: if you can't get the discard dialog and the draft saved, navigate to the event and delete it before reporting completion. Tell the user.
- **Timezone confusion**: if attendees are in multiple timezones, Calendar shows times in the *user's* local zone. Always state the timezone explicitly in the output, and call out attendees whose local time is outside their working hours.
- **Recurring conflicts misread as one-offs**: if an event title appears every week at the same slot in the grid, treat it as recurring (more likely movable for 1:1s, less likely for team meetings).

## API path details

When `freebusy.py` runs successfully, the JSON it emits has this shape:

```json
{
  "attendees": ["alice@example.com"],
  "range": {"start": "...", "end": "..."},
  "duration_minutes": 60,
  "busy": {"alice@example.com": [{"start": "...", "end": "..."}]},
  "errors": {},
  "candidate_slots": [
    {"start": "...", "end": "...", "conflicts": []},
    {"start": "...", "end": "...", "conflicts": ["alice@example.com"]}
  ]
}
```

Slots are pre-sorted by conflict count ascending. Your job is to:
1. Surface the all-free slots first (`conflicts: []`).
2. For the partially-conflicted slots, apply the same ranking rules described above for the browser path (lunch avoidance, day-edge avoidance, earlier-in-range preference).
3. Note that `freebusy` scope returns no event titles, so movability scoring isn't possible from API data alone — every conflict is treated as "unknown — assume fixed." If richer ranking is needed, the scope would need to widen to `calendar.events.readonly` (warrants a separate IT request — see SETUP.md).

When `freebusy.py` exits non-zero:

- **"No usable credentials" / "Missing google_oauth_client.json"** — the user hasn't set up the API path on this machine. Point them at `SETUP.md` (TL;DR section for fresh-machine repeat, or "From scratch" if first time ever). Don't silently fall back to the browser path on a fresh machine — that masks the real fix.
- **`RefreshError: invalid_grant`** — cached token is dead. Tell the user to `rm ~/.config/ai-seal-tools/google_token.json` and re-run; the helper will redo consent.
- **`Calendar API has not been used in project N`** — Calendar API isn't enabled on the project. The user runs `gcloud services enable calendar-json.googleapis.com --project=<id>`.
- **`Access blocked` / `org hasn't approved this app`** — Workspace admin policy is rejecting the OAuth client. The OAuth Desktop path is dead for them; point at SETUP.md's "Alternative auth paths → Service account + DWD" section.
- **HTTP 403 `notFound` for a specific attendee** — that user's calendar isn't visible to the requester. Include this attendee under `errors` in the user-facing notes and continue ranking on the remaining attendees if any are usable.
- **Any other error** — surface it directly to the user with the error text. Don't try to mask with the browser fallback; auth state is worth fixing, not bypassing.

The browser path is the right fallback only when the user *intentionally* hasn't set up API credentials (e.g., they're on a machine where they don't want to bother with config). On any machine where setup was attempted, treat auth errors as bugs to surface.
