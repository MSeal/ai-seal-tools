---
name: get-google-ai-help
description: Ask Google's UI-based AI (the AI Overview at the top of search results) a directed question. Use when Claude's training likely doesn't cover the topic, when current/fresh information matters, or when you want a second opinion sourced from Google's live index. Argument is the question to ask.
---

# Get Google AI Help

Send a question to google.com, capture the AI Overview answer, return it.

Follows the snapshot-driven / vision-fallback discipline described in `prompts/browsing.md`. Read that first if you haven't internalized it.

## Inputs

- `$ARGUMENTS` — the question to ask Google. If empty, ask the user once for the question, then proceed.

## Steps

1. `browser_navigate("https://www.google.com")`.
2. `browser_snapshot()`. Locate the main search input — typically a `combobox` or `textbox` with an accessible name like "Search" or "Search Google or type a URL".
   - If a consent / cookie banner blocks the page, accept it (look for "Accept all" or equivalent) and re-snapshot before proceeding.
3. Type the question into the search input and submit. Most Playwright MCP versions support `browser_type(ref=..., text=..., submit=true)`. If submit-on-type isn't available, type then `browser_press_key("Enter")`.
4. `browser_wait_for(time=2)` (Google's AI Overview can stream in over a second or two). Then `browser_snapshot()`.
5. Find the AI Overview block. It usually appears at the very top of results, headed by text like "AI Overview" or "AI-powered overview". Extract the answer text from that region.
6. If the AI Overview is **not** present in the snapshot:
   - Take a `browser_take_screenshot()` to confirm whether it's missing vs. just unlabeled.
   - If visually missing: fall back to summarizing the first 3 organic results (title + snippet, condensed into a coherent answer). Mark the source clearly as "Top results summary, no AI Overview shown".
   - If visually present but the snapshot didn't capture it cleanly: extract the answer from the screenshot using vision.
7. `browser_close()`.

## Output format

Reply to the user with:

```
Question: <the question>
Source: <AI Overview | Top results summary>

<the answer, 1–3 short paragraphs>

Caveats: <anything notable — consent dialog hit, captcha, partial answer, links worth following, etc.>
```

Keep the answer faithful to what Google returned. Do not augment with your own knowledge unless explicitly asked — the whole point of this skill is to surface what Google has that you might not.

## Failure modes to watch for

- **Captcha / "unusual traffic" page**: stop, screenshot, report to the user. Don't retry in a tight loop.
- **Consent banner loops**: accept once; if the banner re-appears after accept, screenshot and report.
- **AI Overview is paywalled / "Sign in to see"**: report that and fall back to organic results.
- **No results / network error**: screenshot, report, exit cleanly via `browser_close()`.
