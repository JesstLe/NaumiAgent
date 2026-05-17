# Phase 3: Agent Tools (Replace browser.py)

## Source: `scripts/mcp_server.js` tool definitions + `scripts/daemon.js` REST endpoints

## Objective

Replace NaumiAgent's current 6 CSS-selector-based browser tools with SoM-based tools matching the daemon's tool surface. These tools are what the Agent calls during reasoning.

## Files to Modify/Create

- `src/naumi_agent/tools/browser/tools.py` — new tool classes
- `src/naumi_agent/tools/browser.py` — replaced with re-exports from tools.py (or removed)

## Tools to Implement

Replace current tools:
- ~~`browser_navigate`~~ → `browser_goto`
- ~~`browser_screenshot`~~ → merged into observe/goto
- ~~`browser_click`~~ → `browser_click` (SoM ID-based)
- ~~`browser_type`~~ → `browser_type` (SoM ID-based)
- ~~`browser_extract`~~ → `browser_observe` (returns elements + page content)
- ~~`browser_get_html`~~ → merged into evaluate

New tools matching daemon's full surface:

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `browser_goto` | Navigate to URL, auto-observe with SoM | `url` |
| `browser_observe` | Observe current page (SoM elements + accessibility tree + page content + tabs + captcha) | — |
| `browser_click` | Click element by SoM ID | `id` (int) |
| `browser_type` | Type text into element by SoM ID | `id` (int), `text`, `submit` (bool) |
| `browser_hover` | Hover on element by SoM ID | `id` (int) |
| `browser_keypress` | Press keyboard key | `key` |
| `browser_scroll` | Scroll page | `direction` (down/up/top/bottom) |
| `browser_evaluate` | Execute JavaScript | `expression` |
| `browser_select_option` | Select dropdown option by SoM ID | `id` (int), `values` (list) |
| `browser_handle_dialog` | Accept/dismiss browser dialog | `action` (accept/dismiss), `prompt_text` |
| `browser_navigate_back` | Go back in browser history | — |
| `browser_tabs` | List/create/close/switch tabs | `action` (list/new/close/select), `index`, `url` |
| `browser_wait_for` | Wait for text/selector | `text`, `text_gone`, `selector`, `timeout` |
| `browser_upload` | Upload files to element | `id` (int), `paths`, `files` (base64) |
| `browser_drag` | Drag element to element | `from_id` (int), `to_id` (int) |
| `browser_drag_file` | Drag files onto drop zone | `to_id` (int), `paths`, `files` (base64) |
| `browser_screenshot` | Take screenshot (base64) | — |
| `browser_debug_state` | Get debug state (console/network/errors) | — |
| `browser_text_layout_audit` | Audit text overflow | `limit`, `selectors`, `overflow_threshold` |
| `browser_cdp_health` | Check CDP endpoint health | `endpoint` |
| `browser_start` | Start browser session | `source` (managed/attached/auto), `cdp_endpoint` |
| `browser_stop` | Stop browser session | — |

## Tool Implementation Pattern

Each tool:
1. Gets the shared `BrowserRuntime` instance (injected via engine)
2. Calls the corresponding runtime method
3. Returns a structured string result for the LLM

All tools must be registered in `engine.py` via `create_browser_tools(runtime)`.

## SoM Response Format

When `browser_goto` or `browser_observe` returns, format the output for the LLM:

```
Page: [title] ([url])
Interactable Elements (SoM):
  [1] <a> "GitHub" (https://github.com)
  [2] <input> "" (placeholder: "Search...")
  [3] <button> "Submit"
  ...
Accessibility Tree:
  - heading "Welcome" [level=1]
  - link "Sign in" [url=/login]
  ...
Page Content:
  Headings: ...
  Paragraphs: ...
```

## Testing

- `tests/unit/test_browser_tools.py`
- Test each tool with mocked BrowserRuntime
- Test SoM response formatting
- Test error handling for missing elements, invalid IDs

## Checklist

- [ ] All 22 tools implemented
- [ ] Old `browser.py` replaced
- [ ] Tools registered in engine
- [ ] SoM output format tested
- [ ] `ruff check` clean
