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
  --emails <comma-separated-emails-including-the-requester> \
  --start  <ISO 8601> \
  --end    <ISO 8601> \
  --duration <minutes> \
  [--impersonate mseal@confluent.io]    # only with service account
  [--top 5]                              # how many ranked slots to return
```

Include the **requester's own email** in `--emails` — their conflicts matter too, and surfacing them helps the requester see what they'd need to move themselves.

**Fall back to the browser path** only when no credentials are configured (the helper exits with a setup message). The browser path follows the snapshot-driven / vision-fallback discipline in `prompts/browsing.md`.

## Inputs

`$ARGUMENTS` — a free-form description of the meeting. Examples:

- `30 min with alice@example.com and bob@example.com this week`
- `1 hr with the platform team Tue or Wed afternoon`
- `45 min sync with Carol, Dave, and Eve before Friday`

Resolve the inputs into a concrete plan:
- **Attendees**: emails preferred; names are OK if Google Calendar's autocomplete will find them in the Confluent directory. Always include the requester (`mseal@confluent.io` unless specified otherwise).
- **Duration**: default 30 min if not specified.
- **Date range**: convert relative phrases to absolute dates using today's date from the system context. Default to the next 5 business days if unspecified.
- **Working hours**: default to 9:00–17:00 local. If multiple timezones are at play, prefer overlap windows and call out the timezone math in the final answer.

If the description is too ambiguous to act on (no attendees, or a window that's clearly nonsensical), ask **one** clarifying question, then proceed.

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
| `meal` / `personal` | 6 | "Lunch", "Coffee", "Workout" | Negotiable for the person; ask politely |
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

Default output structure:

```
Meeting: <duration> with <attendees> between <start date> and <end date> (<timezone>)

Top recommendations:

1. **<Day, Date> <start>–<end>** — Score <score>/100
   <one-line reason: "All free" / "1 movable conflict" / etc.>

2. **<Day, Date> <start>–<end>** — Score <score>/100
   <one-line reason>

   *Ask Alice:*
   > <a 2–4 sentence pre-formatted message, see rendering rules below>

3. ...

Notes:
- <calendars not visible, attendees declined, timezone math, etc.>
```

### Rendering rules

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
