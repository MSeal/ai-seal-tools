---
name: find-meeting-time
description: Find the best time to meet with a set of company colleagues by inspecting Google Calendar availability. Surfaces ranked slots with quality scores, classifies conflicting events by how movable they are (focus blocks → easy; customer meetings → hard), respects personal scheduling preferences (engine-level config in `~/.config/ai-seal-tools/find-meeting-time/config.yaml`; subjective prose preferences in `preferences.md` alongside it), and renders a pre-formatted ask-message for each conflict so the user can decide whether to ping the conflicting attendee before committing. Two execution paths: an API path via freebusy.py (preferred, uses calendar.events.readonly scope — see SETUP.md) and a Playwright browser fallback. Argument is a free-form description of the meeting (attendees, duration, date range).
---

# Find Meeting Time

Answer "when can we meet?" — and, when nothing is fully free, "what's the least bad time, and what would I need to ask whom to make it work?"

## Execution paths

**Prefer the API path** when `~/.config/ai-seal-tools/google_oauth_client.json` (or service account at `google_service_account.json`) is present.

```bash
UV_NO_CONFIG=1 uv run --script "$(dirname "$0")/freebusy.py" \
  --emails <required-attendees-comma-separated-including-the-requester> \
  [--optional <nice-to-have-attendees-comma-separated>] \
  --start  <ISO 8601> \
  --end    <ISO 8601> \
  --duration <minutes> \
  [--impersonate mseal@confluent.io]    # only with service account
  [--top 5]                              # how many ranked slots to return
```

Include the **requester's own email** in `--emails` — their conflicts matter too, and surfacing them helps the requester see what they'd need to move themselves.

**Required vs optional attendees.** Anyone in `--emails` is "must-have": their conflicts apply the full penalty and a slot without them all is unbookable in practice. Anyone in `--optional` is "nice-to-have": their calendars are still queried (so we know if they're free), but their conflicts apply a reduced penalty (`optional_attendee_penalty_multiplier` in `score_weights.yaml`, default 0.3). Use the user's language to decide — phrases like "would be great if X can join", "if Carol's free", "FYI to Dave" all signal optional. Conflicts attributed to optional attendees show `[optional]` in their score_breakdown label.

**Fall back to the browser path** only when no credentials are configured (the helper exits with a setup message). The browser path follows the snapshot-driven / vision-fallback discipline in `prompts/browsing.md`.

## Inputs

`$ARGUMENTS` — a free-form description of the meeting. Examples:

- `30 min with alice@example.com and bob@example.com this week`
- `1 hr with @eve and #dtx-eng Tue or Wed afternoon`
- `45 min sync with Carol, Dave, and Eve before Friday`

Resolve the inputs into a concrete plan:
- **Attendees**: emails preferred; names are OK if Google Calendar's autocomplete will find them in the Confluent directory. Always include the requester (`mseal@confluent.io` unless specified otherwise).
- **Slack references** (`@handle` and `#channel`): resolve to emails *before* invoking `freebusy.py`. See the "Resolving Slack references" section below.
- **Duration**: default 30 min if not specified.
- **Date range**: convert relative phrases to absolute dates using today's date from the system context. **Always pass `--start >= now`**: never query for slots in the past. If the user says "this week" and it's already mid-afternoon Friday, snap `--start` to the next business-day morning rather than rewinding to Monday. **Never propose a slot whose start time has already passed** at the time of response. Default to the next 5 business days if unspecified.
- **Working hours**: default to 9:00–17:00 local. If multiple timezones are at play, prefer overlap windows and call out the timezone math in the final answer.
- **Attendee timezones**: Calendar freebusy data doesn't reliably surface per-attendee TZ. Before proposing slots, check the `timezone` field for each attendee in `people.yaml` (see the resolution-order section below). If any attendee is missing a TZ, look up their Glean `location` and record it via `record_person_name.py --location <Glean-string>` — the TZ is inferred via `timezone_map.py`. When TZs differ, flag the math inline in the rendered answer (e.g., "3:30 PM PT = 11:30 PM UK") so the user doesn't have to convert.

If the description is too ambiguous to act on (no attendees, or a window that's clearly nonsensical), ask **one** clarifying question, then proceed.

## Resolving Slack references (`@handle`, `#channel`)

When the user names attendees via Slack handles or channels, resolve them to emails *before* calling `freebusy.py`. The cache lives at `~/.config/ai-seal-tools/find-meeting-time/slack_refs.yaml` (Drive-backed, gitignored). Workflow:

1. **Read the cache** with `Read(<config.local>/find-meeting-time/slack_refs.yaml)`. For each `@handle` and `#channel` in the user's request, check the `handles:` and `channels:` sections.
2. **For cache hits**, use the cached value directly:
   - `@eve` → `handles.eve.email`
   - `#dtx-eng` → `channels.dtx-eng.members` (list of emails)
3. **For cache misses, look up via Glean**:
   - `@handle` → `mcp__glean__search` with `app=people` and `query=<handle>`. Read the `email` field from the top result. Cache it with `record_slack_ref.py handle --handle <h> --email <e> --source glean`.
   - `#channel` → `mcp__glean__search` with `app=slack` and `channel=<name>` and `sort_by_recency=true`, ~20 results. Extract unique author emails. Cache with `record_slack_ref.py channel --name <n> --members <emails-csv> --source glean --note "best-effort from recent authors"`.
4. **Handle ambiguity**: if a `@handle` Glean search returns multiple plausible matches, ask the user which person they meant rather than guessing. If a `#channel` search returns zero authors, surface that and ask the user to list the people manually.
5. **For Slack-message rendering** (the `@mention` gap in the Slack message templates below), use `reverse_email_to_handle` in `slack_refs.py` — given an email from a conflict's `conflict_attendees`, look up the cached handle so the rendered DM uses the right `@`-mention.

A real Slack MCP would replace the Glean fallback with authoritative lookups; the cache layout and CLI stay the same. Until then, channel-member resolution is *best-effort* and Claude should mention in the Notes when relying on inferred membership.

## API path: helper output

`freebusy.py` returns JSON of this shape:

```json
{
  "attendees": ["mseal@confluent.io", "alice@example.com"],
  "range": {"start": "...", "end": "..."},
  "duration_minutes": 60,
  "working_hours": {"start": 9, "end": 17},
  "errors": {},
  "total_slots_considered": 312,
  "ranked_slots": [
    {
      "start": "2026-05-20T15:30:00-07:00",
      "end":   "2026-05-20T16:30:00-07:00",
      "score": 100,
      "score_breakdown": [],
      "conflicts": []
    },
    {
      "start": "2026-05-19T16:00:00-07:00",
      "end":   "2026-05-19T17:00:00-07:00",
      "score": 95,
      "score_breakdown": [{"label": "day-edge (late)", "delta": -5}],
      "conflicts": []
    },
    {
      "start": "2026-05-21T10:00:00-07:00",
      "end":   "2026-05-21T11:00:00-07:00",
      "score": 60,
      "score_breakdown": [
        {"label": "conflict: alice@example.com (one_on_one)", "delta": -10}
      ],
      "conflicts": [
        {
          "attendee": "alice@example.com",
          "conflict": {
            "visible": true,
            "summary": "Alice / Bob 1:1",
            "category": "one_on_one",
            "movability": 8,
            "recurring": true,
            "recurring_event_id": "abc123_R20260521T170000",
            "frequency_in_window": 4,
            "status": "confirmed",
            "is_all_day": false,
            "attendee_count": 2,
            "conflict_start": "2026-05-21T10:00:00-07:00",
            "conflict_end":   "2026-05-21T10:30:00-07:00"
          }
        }
      ]
    }
  ]
}
```

Key points:
- `ranked_slots` is pre-sorted by score (descending) and deduped (no two slots overlap, no two share the same conflict signature).
- `score` is 0–100. Base is 100; conflicts subtract `(10 - movability) × 5`; structural penalties (lunch overlap, day-edges) subtract 5–10. The helper applies **only structural penalties** — subjective rules come from `preferences.md` (see next section).
- `score_breakdown` lists every structural penalty. Use the labels to explain rankings rather than just emit a number.
- `config_path` and `preferences_path` indicate which personal config files (if any) are in play for this run.
- `conflict.frequency_in_window` is how many times this recurring series appears in the queried window (per attendee). Use it to write ask-messages with the right tone:
  - `frequency_in_window >= 4` in a 2-week window → weekly or more frequent. Phrase asks as "could we skip this week's instance?" — the attendee has many more chances.
  - `frequency_in_window == 1` for a recurring event → monthly cadence or rarer. Phrase asks as "could we move it?" — skipping costs them a month of catch-up.
  - For one-offs (`recurring: false`, `frequency_in_window: 1`), don't make recurrence-based assumptions — just refer to the event by title.
- `score_breakdown` may include a `lead time: ...` entry when a slot is too close to "now" (helper's `now` reflected in the top-level `now` output field). Two-tier penalty: heavy within the hour (linearly scaling from −40 at now down to −10 at +60min) and lighter same-day (flat −10 for the rest of today). Tomorrow+ slots are unpenalized. If you see this entry on a top-ranked slot, mention the short notice in your user-facing explanation — and ideally suggest a next-day option in the same response.
- `conflict.outcome_history` is the per-(event, attendee) record of past asks from `outcomes.jsonl`. When non-null, it has counts like `{"moved": 3, "declined": 1, "last_outcome": "moved"}`. Use it to:
  - Cite track record in ask-messages: "We've moved this twice already this year — happy to do the rescheduling work again."
  - Soften repeat declines: if `declined >= 2`, don't keep asking the same way. Propose a different slot or a different attendee.
  - The helper already applies a score adjustment (`learned: ... ±N` line in `score_breakdown`); your job is to phrase the message in light of the history, not redo the math.

## Populating seniority for unknown attendees

When a top-ranked slot's conflict shows attendees you haven't cached
seniority for, optionally look them up via Glean and write to the cache
so future runs apply the leadership-tier penalty correctly.

Signal that a lookup might be valuable:
- `conflict.conflict_attendees` has emails (lower-cased) that aren't in
  `seniority.yaml` (visible in the helper's output as `seniority_entries_loaded`).
- The conflict is on a top-3 slot and you're about to draft an ask-message —
  if the meeting includes a senior org member, the framing changes.

Workflow:
1. For each unknown email on a top conflict, call `mcp__glean__search`
   with `app=people` to fetch the profile. Extract `title`,
   `department`, `manager.totalReportsCount`.
2. Invoke `record_seniority.py` with the extracted fields:

   ```bash
   uv run --script skills/find-meeting-time/record_seniority.py \
     --email some-director@example.com \
     --title "Director II, Engineering" \
     --department "Engineering" \
     --total-reports-count 42 \
     --source glean
   ```

3. Next run automatically picks up the new tier.

Don't look up every conflict attendee — only the ones on actually-ranked
top slots, and only when the meeting purpose suggests seniority might
matter (cross-team coordination, anything tied to org leadership).
Sparse curation. The user can always run record_seniority.py by hand
for entries they want pinned.

## Booking the chosen slot

After the user explicitly says "book it" / "schedule it" / "create the event" / "send the invite" (or equivalent), materialize the slot as a real Calendar event.

### Default: hybrid path with fresh Zoom add-on

**For any meeting that needs a video call, attach Zoom via the hybrid path.** This is the *default*, not a special-case. Every booked event gets a unique, real-Zoom-add-on URL — matching how the user books manually. Don't reach for `--conference zoom` (personal room), `--conference zoom-pool`, or anything else unless the user explicitly asks.

**NEVER attach Google Meet.** Not as a default, not as a fallback when Zoom can't be attached, not as a "good enough" substitute. `--conference meet` exists in `create_event.py` for legacy callers but must not be invoked from this skill. If Zoom can't go on the event for any reason — Playwright MCP not connected, Google SSO stale, Zoom add-on missing from the menu, dispatch hangs, Save warns — **stop and tell the user what's wrong.** Do not silently swap conferencing vendors.

**When Playwright MCP isn't connected** (no `browser_*` tools in your tool list), the hybrid path can't run. Don't proceed by falling back to Meet. Instead, surface the situation:

> "Playwright MCP isn't connected in this session — run `/mcp` to bring it up, or I can create the bare event now and you'd need to attach Zoom yourself in the Calendar UI (~2 clicks)."

Wait for direction before doing anything that touches attendees' calendars.

### When to use the API path directly

`create_event.py` alone (no Playwright follow-up) is reserved for **explicit non-Zoom-add-on cases the user has stated**:

- In-person event, hold, or focus block → `--conference none`.
- User explicitly says "use my personal Zoom" → `--conference zoom` (requires `zoom_personal_meeting_url` in config).
- User explicitly says "rotate through the zoom pool" → `--conference zoom-pool`.

Anything else routes to the hybrid path.

### Rules of engagement (both paths)

- **Confirm before sending invites to others.** Picking a slot in chat ("Mon 1:30 works") is not the same as authorizing invites to go out. If the user hasn't used an explicit booking verb, propose what you'd send (summary, attendees, conference type) and wait for a "yes". Once they say "book it" with an explicit verb, you have consent for that single event — don't keep booking subsequent events implicitly.
- **Use a summary that reads well in invitees' inboxes.** Not "[Meeting]" or "Quick chat" — be specific: "AI tooling sync — you + eve", "Hiring debrief: <candidate>". Pull from the conversation context.
- **Pass attendee emails resolved by the Slack-ref tool**, not raw `@handles`. Every attendee should be `<local>@example.com` form.
- **Echo the result** with the event's HTML link (click-to-edit) and the conference join URL.
- **Don't auto-log this as an outcome** — `record_outcome.py` is for tracking ask-to-move outcomes, not event creation.

### API path (explicit non-Zoom-add-on cases only)

Only reach for this when the user has named one of the cases in **"When to use the API path directly"** above. For any video meeting where the user hasn't named a specific conferencing kind, use the hybrid path instead.

```bash
uv run --script "$(dirname "$0")/create_event.py" \
  --start <slot start ISO 8601 with offset> \
  --end   <slot end ISO 8601 with offset> \
  --summary "<event title — usually derived from the meeting purpose>" \
  --attendees <comma-separated required-attendee emails> \
  [--conference zoom|zoom-pool|none]        # do NOT use 'meet'
  [--zoom-url <ad-hoc Zoom URL>]            # override personal/pool
  [--description "<context>"]
  [--dry-run]                                # preview body without calling API
```

**Conference choice:**
- **`none`**: in-person events, holds, focus blocks. The common reason to use this script directly.
- **`zoom`**: only when the user explicitly asked for their personal Zoom room. Requires `zoom_personal_meeting_url` in config; if it's missing, **don't fall back to Meet** — surface the gap and route to the hybrid path (which gets fresh Zoom without needing personal URL config).
- **`zoom-pool`**: only when the user explicitly named the static-pool rotation (typical reason: back-to-back / parallel calls).
- **`meet`**: **never used from this skill.** The CLI option exists for legacy callers; do not invoke it as a default, a fallback, or a substitute.
- **`--zoom-url`**: ad-hoc override only when the user provides a specific URL.

If `conference_status` in the response flags an issue (missing join URL when one was expected), **do not retry with `--conference meet`**. Surface the failure, explain what's broken, and either route to the hybrid path (for a fresh Zoom add-on URL) or let the user fix conferencing manually.

The first run after upgrading from a read-only `freebusy.py` will pop a browser for the broader write scope (the auth path's scope-mismatch detector handles it). User clicks through once; new token caches separately at `~/.config/ai-seal-tools/credentials/google_calendar_write_token.json`.

### Hybrid path (Plan B — API create + browser Zoom attach)

Two API calls bracket a 3-click Playwright sub-step. The API does all the boring stuff (title, times, attendees) — Playwright is only responsible for the one operation the public API can't do: triggering the Zoom Workspace add-on. If Playwright fails halfway, the event already exists on the user's calendar with no conferencing; recovery is a manual click in the user's own browser tab rather than a debug session.

Follows the snapshot-driven / vision-fallback discipline in `prompts/browsing.md`.

1. **Create the bare event via the API.** Use `create_event.py` exactly like the API path above, but pass `--conference none`. Capture the `event_id` from the JSON response — every subsequent step needs it.
   ```bash
   uv run --script "$(dirname "$0")/create_event.py" \
     --start <iso> --end <iso> \
     --summary "<title>" --attendees <emails> \
     --conference none
   ```
   If the user has stipulated "don't send invites yet" or this is a self-only hold, the create still succeeds; Playwright will only fire if you decide to proceed.

2. **Open the event editor in Playwright** by navigating to `https://calendar.google.com/calendar/u/0/r/eventedit/<eid>`, where `<eid>` is the base64-encoded `event_id + " " + calendar_email` — the htmlLink returned by `create_event.py` already contains the correct `?eid=<base64>` parameter. Extract it with `book_browser_helpers.extract_eid_from_url`.

   - If you land on the calendar grid + a popover instead of the full editor, the URL form has changed; fall back to `browser_navigate("https://calendar.google.com")`, snapshot, click the event tile, then "Open detailed view" / "Edit".
   - If `browser_navigate` returns `Target page, context or browser has been closed`, that's the **one expected error after the sign-in helper ran** (see sign-in flow below). The MCP discarded its stale browser handle on the failure and will launch a fresh Chrome on retry. Call `browser_navigate` once more with the same URL — second call succeeds against the now-signed-in profile.
   - If you land on a sign-in page, `accounts.google.com`, or the `workspace.google.com/.../calendar/` marketing landing → the persistent profile has no valid Google session. **The MCP runs headless** (no visible window), so SSO can't happen inside it. Tell the user:
     > "Your Google session for the Playwright profile has expired. Run `playwright-sign-in` in a terminal — that'll open a headed Chrome with our profile, you complete SSO + 2FA, close the window. Then tell me 'signed in' and I'll retry the booking. No need to restart Claude Code."

     Wait for the user's confirmation. When they say signed in, retry from step 2 (expect the one stale-handle error described above, then success).
     - Do NOT type credentials yourself; do NOT call `browser_navigate` to drive the sign-in — the headless Chrome can't render the SSO 2FA flow.
     - With Confluent's Workspace session policy, this is a one-shot every several hours/days, not per-booking.
     - This flow is enabled by the npx-mcp-shim PATH interception (see `README.md` → Playwright MCP persistent profile). No IT allowlist change needed; the `.mcp.json` command still literal-matches the existing MDM-approved Playwright MCP entry.

3. **Click the conferencing dropdown.** Find the "Add video conferencing" button (label rotates: "Add Google Meet video conferencing" if Meet is default). Click it; a `Conferencing solutions` menu opens.

4. **Click "Zoom Meeting"** in the menu. `browser_wait_for(text="Join Zoom Meeting", time=8)` for the add-on dispatch to complete.

   - If "Zoom Meeting" isn't in the menu, the add-on isn't installed for this account. Stop, tell the user, leave the event in place (they can manually add Zoom themselves or accept the no-conf event).

5. **Click Save** (top right of the editor). If there are attendees, a "Would you like to send update emails to existing Google Calendar guests?" dialog appears — click **Don't send**. The initial `create_event.py` invite has already gone out via `sendUpdates="all"`, and the Zoom URL is now on the server-side event (attendees see it when they click through from their existing invite). A second purely-Zoom-link update email is noise. See memory `skip-zoom-update-email`. Exception: only click Send if the user has explicitly asked you to re-notify attendees.

6. **Re-query the event via the API** to capture the attached conferenceData:
   ```bash
   uv run --script "$(dirname "$0")/get_event.py" <event_id> --requested-conference zoom
   ```
   The output shape matches `create_event.py`'s response: `event_id`, `html_link`, `join_url`, `conference_solution`, `conference_status`, etc.

7. **Echo the result** to the user. If `conference_status` shows "no conference entry points attached" the Playwright click silently failed — tell the user the event exists but conferencing didn't attach; they can fix it in their own Calendar tab.

**Failure modes specific to this path:**
- **Playwright MCP not connected (no `browser_*` tools in tool list)** → can't run the path at all. **Do not fall back to `--conference meet`.** Stop before any API call and ask the user to either run `/mcp` to bring up Playwright, or to attach Zoom themselves in the Calendar UI after you create the bare event.
- **Step 2 redirects to sign-in** → drive interactive sign-in (sub-bullets above), then retry.
- **Step 4: "Zoom Meeting" missing from menu** → add-on not installed. Event already exists; surface and leave alone (do NOT swap to Meet).
- **Step 4: dispatch hangs >15s** → screenshot, report. The Zoom Apps Script deployment may be transiently slow. Don't substitute Meet.
- **Step 5: Save fails with overlap warning** → conflict probe in `freebusy.py` may have had stale data; the event was never created, so re-run from step 1 with a new slot.
- **Step 6: get_event returns no conferenceData** → Playwright click landed but Save didn't commit it (rare; usually a network blip). The event exists titled/timed correctly; tell the user and let them retry the attach manually.

**Why this lives separately:** Google's public Calendar API restricts `createRequest.conferenceSolutionKey.type` to `hangoutsMeet` only — no path to invoke a Workspace add-on's conference dispatch from the API. The internal RPC the UI uses requires session cookies + SAPISIDHASH auth that are impractical to forge from a script (and unmaintainable across Google's wire-format rotations). Driving the UI for *just* the add-on click is the stable way to reach that dispatch until Google exposes it publicly.

**Helpers:** `book_browser_helpers.py` carries the deterministic pieces (parsing `?eid=` from a Calendar URL, decoding the eid base64, extracting a Zoom URL from a snapshot, shaping a response when you skip the get_event re-query). Unit-tested in `tests/test_book_browser_helpers.py`.

## Moving an existing event

When the user already has the meeting on their calendar and just wants to **shift it** to a slot you ranked (rather than create a brand-new event with the same attendees), use `move_event.py`. Examples of the user's intent:

- "Move my existing X meeting to that slot"
- "Reschedule the Y review to Wednesday"
- "Push the Foo sync to next week"

This is preferred over delete-and-recreate because it:
- Sends a single "event time changed" notification rather than a cancellation + a new invite (less noise for attendees).
- Preserves the event ID — third-party integrations (Zoom Workspace, recording bots, agendas linked from Slack) keep working.
- Preserves attendee RSVPs and any extra metadata (description, location, conferenceData, recurrence) that `events.patch` doesn't touch.

```bash
uv run --script "$(dirname "30-minute")/move_event.py" <event_id> \
  --start <new start ISO 8601 with offset> \
  --end   <new end ISO 8601 with offset> \
  [--calendar primary] \
  [--send-updates all|externalOnly|none]   # default 'all'
  [--dry-run]
```

The output shape matches `create_event.summarize_response` — `event_id`, `html_link`, `join_url`, attendees, and the new start/end — so the post-action user-facing summary is consistent between a fresh booking and a move.

**Finding the event ID.** If the user references the meeting by title rather than ID, look it up via `svc.events().list(..., q="<keyword>", singleEvents=True)` first — same auth/scope as `move_event.py`. The Capybara-meeting move pattern: search Friday's events for the title, extract the `id`, then call `move_event.py`.

**Rules of engagement.** Same as booking — confirm before sending update emails to others. Calling `move_event.py` issues a reschedule notice to every attendee by default. If the user picked a slot but hasn't explicitly said "move it" / "reschedule it" / "shift it", propose the patch first and wait for a yes.

**Don't try to move recurring-series instances** with this script unless the user specifies whether they mean *this one occurrence* or *the whole series*. The Calendar API has different IDs (`<series>_R<YYYYMMDDTHHMMSS>` for instances vs the master ID for the series); patching the wrong one moves the wrong thing. Ask first.

## Logging outcomes after an ask

After the user reports back on how an ask went, log the outcome with `record_outcome.py` so future runs learn from it:

```bash
uv run --script skills/find-meeting-time/record_outcome.py \
  --attendee <attendee-email> \
  --outcome <moved|agreed|declined|scheduled|skipped> \
  --event-fingerprint <fingerprint-from-conflict> \
  [--summary "<event title>"] \
  [--note "<free-form context>"]
```

When to log:
- User says "Alice agreed to move" → log `moved` (or `agreed`; same effect)
- User says "Bob said no" → log `declined`
- User confirms they're scheduling the meeting at a slot → optionally log `scheduled` for each conflict, useful for audit
- User picks a different slot without asking → log nothing (no signal)

The `event-fingerprint` is the `fingerprint` field from the conflict you discussed. Pass it directly; don't try to reconstruct.

Default: log proactively when the outcome is obvious from the user's message. Don't ask permission to log — the user can `tail -f config.local/find-meeting-time/outcomes.jsonl` to inspect history, or delete bad entries by editing the file.
- Each entry in `conflicts` is an *ask-context*: enough structured data for you to compose a pre-formatted message to that attendee.

## Personal preferences — read `preferences.md` before ranking

After the helper returns and **before** writing the final user-facing answer, read `preferences.md` if `preferences_path` in the helper output is non-null:

```
Read(preferences_path)
```

The file is free-form prose. Apply it as follows:

1. **Re-rank.** The helper's score is a starting point based on structural rules only. The user's prose may reorder slots — e.g., "Mondays are recovery days" pushes Monday slots down; "Tue/Thu 9–11 PT is deep work" should make those slots score worse even if the helper marked them all-free.
2. **Use exception conditions.** Preferences often have escape hatches ("...unless every alternative this week is worse"). Apply the rule literally; if the conditions trigger, lift the penalty.
3. **Cite when applying.** When a preference changes ranking, quote (or paraphrase) the relevant sentence in user-facing output so the user can trace the decision: *"Ranked Wed lower because you said Mondays are recovery days... wait, that's Wed not Monday, ignore."* Saying the reasoning out loud helps catch mis-application too.
4. **Use per-person notes for conflict tone.** If `preferences.md` has notes about a specific attendee (e.g., "Sorabh moves 1:1s easily"), use that to adjust the ask-message style rather than the generic template.
5. **Use ask-tone preferences.** Apply "peers casual / senior apologetic / external formal" if the user specified.

If `preferences_path` is null, the helper's structural ranking stands — use the default templates below.

## Movability categories

| Category | Score | Example titles | How to talk about it |
|---|---|---|---|
| `focus_block` | 10 | "Focus time", "Deep work", "No meetings" | Trivially movable — it's a self-imposed block |
| `personal_hold` | 9 | "Hold", "Tentative", "Placeholder", "Block" | Easy to shift; the holder is signalling flexibility |
| `tentative` (status) | 9 | (any title, status=tentative) | Not yet committed |
| `one_on_one` | 8 | "1:1", "1/1", "Alice/Bob" | Recurring 1:1s are typically the easiest real meeting to shift |
| `travel_block` | 7 | "Travel", "Commute", "WFH" | Reasonably flexible |
| `meal` (lunch/coffee) | 6 | "Lunch", "Coffee" | Negotiable for the person; ask politely |
| `meal` (breakfast/dinner) | 3 | "Breakfast", "Dinner" | Bookend-of-day; typically anchored by family routine (school drop-off, kids' bedtime). Treat as ~fixed and don't ask to move. |
| `personal` | 6 | "Workout", "Gym", "Yoga" | Negotiable for the person; ask politely |
| `generic_meeting` | 5 | Anything not matched | Default — unknown movability |
| `opaque` | 5 | (summary hidden by sharing) | Can't classify automatically; user has to ask the attendee |
| `team_meeting`, `team_standup` | 5 | "Weekly sync", "Team standup" | Disrupts a group — possible but coordination cost |
| `customer_meeting` | 2 | "Customer call", "Client sync" | External; usually fixed |
| `exec_sync` | 2 | "Exec review", "Leadership sync" | Hard to move |
| `interview` | 1 | "Phone screen", "Onsite" | Don't ask to move |
| `all_hands` | 1 | "All-hands", "Town hall" | Fixed |
| `ooo` / `all_day_block` | 0–1 | "OOO", "PTO", all-day events | Treat as immovable; don't even ask |

These are heuristics. The user's judgment overrides — if Alice's "1:1" is with her CEO, it's not actually movable.

## Rendering the answer

**Always render each top slot via `render_slot.py`** — it produces a consistent header + ASCII timeline that shows the layout of conflicts across all attendees. Don't hand-format slot cards; the renderer's contract is what tests lock in.

**This is a hard requirement, not a default.** Every user-facing answer that proposes one or more times (or argues no time is workable) MUST include the rendered slot cards. There are no exceptions:

- **Don't skip when all scores are 0 / no viable slot.** The timeline visualization is *exactly* what makes structural patterns (offsite blocks spanning all attendees, OOO walls, opaque columns) legible at a glance. A textual table can hide that.
- **Don't skip on "obvious" answers** (e.g., one clean slot, one attendee). The slot card + "Your day" band carry surrounding-context information (adjacent meetings, back-to-back risk, lunch overlap) that flat text doesn't.
- **Don't skip on probes / "let me just check Friday too" turns.** If you call `freebusy.py`, you also call `render_slot.py`. A summary table for follow-up questions is fine *in addition to* the slot cards, not in place of them.
- **Don't skip the augmentation step.** Always splice `context_events` (via `events_around.py`) into the slot dict *before* rendering, so each card includes the requester's ±2h "Your day" band with the slot marker and adjacent events. The surrounding context is half the value — without it, the reader can't see whether the proposed slot is back-to-back with something heavy.

A final answer that proposes times without rendered slot cards is incomplete and should be revised before sending.

```bash
# 1. Save freebusy output to a temp file.
TMP=$(mktemp); ...freebusy.py ... > $TMP

# 2. For each top slot, fetch the requester's ±2h context events and
#    splice them into the slot dict as `context_events`. The renderer
#    automatically adds a "Your day" band + slot marker + adjacent
#    events list when this field is present.
TMP_AUG=$(mktemp)
uv run python -c "
import json, sys, subprocess
data = json.load(open('$TMP'))
for slot in data.get('ranked_slots', [])[:5]:
    out = subprocess.check_output([
        'uv', 'run', '--script',
        '$(dirname \"$0\")/events_around.py',
        '--slot-start', slot['start'],
        '--slot-end',   slot['end'],
        '--email',      'mseal@confluent.io',
    ], text=True)
    slot['context_events'] = json.loads(out)['context_events']
json.dump(data, open('$TMP_AUG', 'w'))
"

# 3. Render the augmented slots.
uv run --script "$(dirname "$0")/render_slot.py" \
  --from "$TMP_AUG" \
  --attendees mseal@confluent.io,alice@example.com \
  --requester mseal@confluent.io \
  --names "mseal@confluent.io=Matthew Seal,alice@example.com=Alice Lee" \
  --top 5
```

The context band defaults to ±2 hours with 30-min ticks. The slot itself is marked with `└── proposed ──┘` underneath the row, so back-to-back vs. clear-pocket is visible at a glance.

**Always pass `--names`** with the full display name for every attendee — using `firstname.lastname` or the email handle as the row label trades scannability for ~no work saved.

**Resolution order for names:**

1. **`people.yaml` cache** (`~/.config/ai-seal-tools/find-meeting-time/people.yaml`). Read it first and pull `people.<email>.name` for each attendee. This is the bulk of the work — the cache covers everyone the requester has met in the last ~30 days (and grows over time).
   ```bash
   uv run python -c "
   import yaml; data = yaml.safe_load(open('$CACHE_PATH').read()) or {}
   people = data.get('people', {})
   for e in ATTENDEES:
       name = people.get(e.lower(), {}).get('name')
       if name: print(f'{e}={name}')
   "
   ```
2. **Glean lookup** for any attendee not in the cache (or whose cache entry has `name: null` / no `timezone`). Use `mcp__glean__chat` with a `What is X's Confluent email?`-style prompt, or `mcp__glean__search` with `app=people`. Extract both `name` and `location` (e.g., `GB Remote United Kingdom`) from the result. After resolving, persist via `record_person_name.py --source glean single --email <e> --name <n> --location "<loc>"` so the next call hits the cache. Timezone is inferred from `location` automatically; pass `--timezone <IANA>` only to override the inference (e.g., `US Remote Washington` → defaults to PT but person is actually in DC).
3. **Email local-part fallback** for anything still unresolved — the renderer does this automatically when a name is missing from the `--names` map.

The cache file is gitignored and Drive-backed. Maintain it with:

```bash
# Top up after meetings happen (idempotent; safe to re-run):
uv run --script scan_recent_attendees.py [--since-days 30 --max-attendees 5]

# Fill in names + locations that Calendar's displayName / freebusy
# didn't surface, after a Glean lookup. Single or bulk-from-stdin:
record_person_name.py --source glean single \
    --email <e> --name "<n>" --location "<Glean-location-string>"
# Bulk accepts either {"email": "name"} (legacy) or
# {"email": {"name": "...", "location": "...", "timezone": "..."}}:
echo '{"e1": {"name": "N1", "location": "GB Remote United Kingdom"}}' \
    | record_person_name.py --source glean bulk
```

Then pass the resolved mapping to the renderer: `"email1=First Last,email2=First Last,…"`. The renderer falls back to email local-part for any unmapped entries — no display crash, just less readable.

The renderer outputs each slot as:

```
N. **<Day, Mon D> · <start>–<end>** (<N> min) — Score <N> · <one-line summary>
```
                    H:MM   H:MM   H:MM   H:MM
you (you)         ─────  ─────  ─────       (no conflict → clean dashes)
alice               ░░░░░  ░░░░░  ─────       "Conflict name" (movability 8)
```
```

Glyphs encode movability at a glance: `─` free, `░` easy (movability 7+), `▓` moderate (4–6), `█` ⚠ fixed (≤ 3), `?` opaque. The requester's row gets a `(you)` annotation in the label and a `← you` marker on any conflict annotation. Above 6 attendees with conflicts the timeline collapses into a flat list — at that density it's a coordination problem, not a scheduling one.

The full structure of your reply, wrapping the slot cards:

```
Meeting: <duration> with <attendees> between <start date> and <end date> (<timezone>)

Top recommendations:

<slot card 1 from render_slot.py>

<slot card 2>

…

Tradeoffs / Notes:
- <inter-slot tradeoff calls (when top scores are within ~15 and have different conflict signatures)>
- <calendars not visible, attendees declined, timezone math, etc.>

*Ask <Name>:*
> <2–4 sentence ask-message — only render for visible+movable conflicts on top-3 slots>
```

### Rendering rules

- **Surface tradeoffs explicitly** when the top slots have *different* conflict signatures and similar scores (within ~15 points). Don't just list them as independent items — render the comparison so the user can decide based on relationships, not just numbers:

  > Two close options, each with a different ask:
  > - **Tue 4–5 PM** (score 75) → ask **Alice** to move her 1:1 with manager (you've successfully moved this before)
  > - **Wed 10–11 AM** (score 72) → ask **Bob AND Carol** to move their team standup
  >
  > Tue is one ask of a person you have an easy track record with; Wed is two asks of people whose meeting is a recurring team disruption. Tue probably wins unless you owe Alice favors.

  When the conflict signatures overlap (same attendee, same event), don't render as a tradeoff — those aren't real alternatives.

- **Top 3 always shown.** Show 4–5 if scores are close (within 15 points of #1).
- **All-free slots first.** Stop at the score floor below 50 unless every option is below.
- **Inline ask-messages only when needed.** Don't render an ask-message for an all-free slot. Render one per *visible* conflict on slots ranked top-3.
- **For opaque conflicts** (`visible: false`), don't render a per-conflict ask. Instead, in the Notes section, mention that the attendee's calendar shows free/busy only and the user will need to ask informally.
- **Don't repeat ask-messages.** If the same attendee has the same recurring conflict on multiple slots, render the ask once with a list of slots-to-confirm.

### Ask-message templates

These are guidance, not literal templates — adapt tone to the meeting purpose the user described and the relationship implied.

**Visible conflict, movable (`movability >= 7`):**

> Hi <Name> — I'm trying to put a <duration-minute> <meeting purpose> on the calendar with <other attendees>, and the slot that works for everyone is <day/time>. That overlaps your "<conflict summary>". Would you be able to shift it, or is it OK to make this work over it?

**Visible conflict, moderate (`movability 4–6`):**

> Hi <Name> — for a <duration-min> <meeting purpose> with <others>, the best slot for the group is <day/time>. I see you have "<conflict summary>" then. Wanted to check whether that's flexible before I send the invite, or whether I should look for another time.

**Visible conflict, low (`movability <= 3`):**

> Don't render a movement-request. Instead, in the user-facing answer say: "Alice has '<conflict>' — likely fixed; consider a different slot."

**Visible conflict with recurring 1:1 specifically:**

> Hi <Name> — for the <meeting purpose> I'm setting up, the only slot that works is <day/time>, which overlaps our standing 1:1. Can we slide it by <suggested duration> or skip it this week?

**Opaque conflict:**

> Don't render an ask-message. In Notes: "<Name>'s calendar shows free/busy only; you'd need to confirm with them directly whether <slot> is workable."

When the requester themselves has the conflict (`attendee == <requester email>`), phrase it as a self-reminder rather than a message to send: "*You'd need to move:* <conflict>".

## Slack-formatted messages

The templates above are email-flavored. When the user says "draft a Slack message" / "give me something to paste into Slack" / "I'll DM them" — switch to Slack-native conventions:

- **Single-asterisk `*bold*`** (not `**bold**`); `_italic_`; `` `code` ``; `> blockquote`. No headers, no nested bullets deeper than one level.
- **Short paragraphs**, usually one sentence each. Slack's narrow column makes long lines hard to scan.
- **First-name @mention** at the start when DM-ing one person (`@alice`) or addressing one in a group thread. We don't have a Slack-handle resolver yet, so use the email's local part as the handle and note any ambiguity in a trailing line ("`@alice` may need a fix — multiple Alices at the company"). Don't render the email address inline.
- **No greeting block** ("Hi Alice — hope you had a good weekend") — Slack DMs skip that. Lead with the ask.
- **End with a friction-reducer**, not a sign-off: "happy to handle the reschedule", "react with what works", "no rush".

### Move-conflict ask (Slack DM)

For asking one attendee to shift a single conflicting meeting so a proposed slot works:

```
Hey @<firstname> — trying to set up a *<duration>m <purpose>* with you and <others, if any>. The slot that works for everyone is *<Day, Time TZ>*, which overlaps your "<conflict summary>".

Could we slide it, or are you OK letting me schedule over it?

Happy to do the rescheduling work if it helps.
```

Tone variants:
- **Peer (default)**: as above.
- **Senior** (`tier >= 3` in seniority.yaml): apologize for the ask, propose an alternative explicitly. "Hey @<firstname> — apologies for the overlap; could we do *<alt slot>* instead, or want me to find another window entirely?"
- **External / customer** (`category == customer_meeting`): don't generate a Slack ask. Slack-asking a customer to move their meeting is a faux pas — surface in the Notes section that the user should reach out via email or their AE.

If the conflict has `outcome_history.moved > 0`, lead with the track record:
> "We've moved this a couple times before and it's worked — same play OK?"

### Options proposal (Slack group post / DM)

For offering 2–3 slot options and letting the recipient(s) pick. Use when no all-free slot exists and you're punting the decision to the group:

```
Hey @<firstname> — proposing *<duration>m <purpose>* at one of these:

• *<Day, Time TZ>* — <one-line caveat if there's a movable conflict>
• *<Day, Time TZ>* — <caveat>
• *<Day, Time TZ>* — <caveat>

React with the slot that works for you (or any that work), or shout if none do.
```

Order options by score (best first). If a slot has a conflict, the caveat should be specific enough that the recipient can self-assess ("overlaps your weekly platform sync; you've moved this before") — but keep it one line. Don't include scores in the message; numerical scoring is internal context, not user-facing chat.

For three-or-more attendee proposals (the multi-attendee case), prefer this format over individual asks — it's lower coordination cost and people can react ⚡-style.

## Error handling

When `freebusy.py` exits non-zero, route to SETUP.md rather than silently falling back to the browser path:

- **"No usable credentials"** — first-time setup. Point at SETUP.md TL;DR / From scratch.
- **`RefreshError: invalid_grant`** — token died. `rm ~/.config/ai-seal-tools/google_token.json` and re-run.
- **`Calendar API has not been used in project N`** — `gcloud services enable calendar-json.googleapis.com --project=<id>`.
- **`Access blocked` / `org hasn't approved this app`** — Workspace admin rejecting OAuth client. Point at SETUP.md Alternative auth paths.
- **403 for specific attendee in `errors`** — that calendar isn't visible; rank with remaining attendees and mention in Notes.
- **`insufficient_scope` / `scope not approved`** — token was issued with an older narrower scope. Delete `google_token.json` and re-run to redo consent with `calendar.events.readonly`.

The browser path is the right fallback only when API setup hasn't been attempted on this machine. On any machine where it has, treat errors as bugs to surface, not failures to mask.

## Browser fallback path

Use only when no API credentials are configured.

1. `browser_navigate("https://calendar.google.com")`. If redirected to a sign-in page, stop and tell the user — they need to sign in to their Confluent Google account in the browser session. Do not attempt to type credentials.
2. `browser_snapshot()`. Confirm you're on the calendar view.
3. Click **Create** → **Event** → **More options** to open the full event editor.
4. In the full editor:
   - Title: `[DRAFT — do not save]`
   - Duration: as requested, on any date in the target range.
   - Guests: add each attendee, wait for directory autocomplete.
5. Switch to **Find a time** tab.
6. Navigate the date range, screenshot the side-by-side grid, identify candidate slots.
7. Apply the same ranking rules described under "Movability categories" — but with vision-driven inference (you can read titles where shared, or fall back to "opaque" otherwise).
8. Close the draft without saving: Esc → **Discard**. Confirm you're back on the main calendar view.
9. `browser_close()`.

Same output format as the API path, but you'll typically have less rich conflict data (no `eventType` field, recurring detection by eyeball, etc.). Mark inferences as "(inferred)" rather than asserted.
