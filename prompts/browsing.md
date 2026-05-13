# Web Browsing Capability

Paste this block into the system prompt of any agent/skill that needs to drive a browser. It assumes the Playwright MCP server (`@playwright/mcp`) is connected — in this repo it's wired up via `.mcp.json` at the root.

---

You can drive a real browser via the Playwright MCP server. Use it for navigation, click-testing, form interaction, and data extraction from sites without a clean API or SDK.

## Primary loop — snapshot-driven

Default to the accessibility tree. It is structured, cheap, and works for the large majority of normal web apps.

1. `browser_navigate(url)` to load the page.
2. `browser_snapshot()` returns an accessibility tree with stable `ref` ids for each element on the current DOM.
3. Act on refs: `browser_click(ref)`, `browser_type(ref, text)`, `browser_select_option(ref, value)`, `browser_hover(ref)`, `browser_press_key(key)`.
4. After **any** state-changing action, re-snapshot. Refs are tied to a snapshot — DOM changes invalidate them.
5. Use `browser_wait_for(text=... | time=...)` between action and re-snapshot when the page is async.

## Fallback path — vision-driven

Switch to screenshots + visual reasoning when the snapshot loop is failing. Common triggers:

- **Canvas / custom rendering**: maps, Figma, design tools, charting widgets — no ARIA tree to act on.
- **Missing labels**: icon-only buttons, custom components with no accessible name. Snapshot shows a generic node with no useful ref.
- **Snapshot is huge or noisy**: large lists, complex SPAs where you can't confidently pick the right ref.
- **Action "succeeded" but nothing changed**: re-snapshot looks identical → the click hit the wrong target.
- **Bot-detection / anti-automation**: page renders differently for headless; only visual inspection tells you.

Fallback steps:

1. `browser_take_screenshot()` to see the page as a user would.
2. Reason about the target from the image (location, label, visual state).
3. Act either by coordinate (`browser_click(x, y)`) or via keyboard (`browser_press_key`) — keyboard is often more reliable for forms and dialogs.
4. Re-screenshot to confirm the result.

## Practical rules

- **Always re-snapshot or re-screenshot after a state change** before the next action. Stale refs are the #1 source of silent failures.
- **One tab unless multi-tab is actually required.** Tab juggling is a debugging tax.
- **Inspect failures, don't retry blindly.** If `browser_click` fails or the page didn't update, take a screenshot before retrying — the cause is usually visible.
- **Read network/console when stuck**: `browser_network_requests()` and `browser_console_messages()` often reveal auth failures, blocked requests, or JS errors that explain why the UI isn't responding.
- **Close the browser when done** (`browser_close`) so processes don't leak.
- **Headed vs. headless**: default config runs headless. For interactive click-testing where you want to watch, launch Playwright MCP with `--headed` (override in `.mcp.json` or pass via env).

## Data extraction shape

For scraping-style tasks:

1. Navigate.
2. Snapshot. If the data you want is in the a11y tree, parse it directly — no model call needed.
3. If structure is buried (custom rendering, weird DOM), screenshot and let the model extract from the image.
4. For long lists / pagination, loop: act → snapshot → extract → next page. Don't accumulate refs across pages.
