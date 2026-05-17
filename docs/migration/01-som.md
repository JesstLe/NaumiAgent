# Phase 1: Set-of-Mark (SoM) + Shared Helpers

## Source: `scripts/shared.js` (430 lines)

## Objective

Port all SoM and shared utility functions into Python. This is the foundation layer that everything else depends on.

## Files to Create

- `src/naumi_agent/tools/browser/__init__.py`
- `src/naumi_agent/tools/browser/som.py`

## Functions to Port

### 1. Interactive Selectors (`INTERACTIVE_SELECTORS`)

```python
INTERACTIVE_SELECTORS = ", ".join([
    "a", "button", "input", "textarea", "select", "details",
    '[role="button"]', '[role="link"]', '[role="menuitem"]',
    '[role="tab"]', '[role="option"]', '[role="switch"]',
    '[role="checkbox"]', '[role="radio"]', '[role="card"]',
    '[role="dialog"]', '[role="treeitem"]',
    '[contenteditable=""]', '[contenteditable="true"]',
    "[onclick]", "[tabindex]", "[data-testid]",
    "[data-action]", "[data-clickable]", "[draggable='true']",
])
```

### 2. `collect_interactable_elements(page, draw_overlays=False) -> list[dict]`

Port of `collectInteractableElements()`. Injects into page, queries all interactive selectors, returns structured data with: id, tag, text, context_html, aria_label, name, role, type, title, href, data_testid, x, y, placeholder. Optionally draws red numbered overlays.

### 3. `clear_overlays(page)`

Removes all `.agent-som-overlay` elements from the page.

### 4. `refresh_target(page, observed_elements, id) -> dict`

Port of `refreshTarget()`. After page changes, re-collects elements and scores candidates against previous observation using `score_candidate()`.

### 5. `score_candidate(previous, candidate) -> float`

Scoring function: tag match (+40), role match (+15), type match (+15), href match (+20), text similarity (*60), placeholder similarity (*30), aria-label similarity (*30), name similarity (*15), title similarity (*15), position distance penalty (-distance/25, max -20).

### 6. `text_similarity(left, right) -> float`

Normalized case-insensitive text comparison: exact match=1, substring=0.6, else=0.

### 7. `collect_page_content(page) -> dict`

Extracts headings (h1-h6 with level), paragraphs (first 10, truncated), lists (first 5), tables (first 3). Returns `{headings, paragraphs, lists, tables}`.

### 8. `detect_captcha_challenge(page) -> list[dict] | None`

Port of `detectCaptchaChallenge()`. Checks for: reCAPTCHA iframe, hCaptcha iframe, Cloudflare Turnstile, generic CAPTCHA elements, text signals ("I'm not a robot", "verify you are human", etc.).

### 9. Storage state encryption/decryption

Port `encrypt_storage_state()`, `decrypt_storage_state()`, `load_storage_state()`, `save_browser_state()` using Python's `cryptography` library (AES-256-GCM). Derive key with SHA-256. Tighten file permissions with `os.chmod(0o600)`.

### 10. `write_base64_files(files) -> list[str]`

Write base64-encoded files to a temp directory. Returns list of written paths.

### 11. `get_select_all_shortcut() -> str`

Returns "Meta+A" on macOS, "Control+A" otherwise.

## Testing

- `tests/unit/test_browser_som.py`
- Test `score_candidate` scoring weights
- Test `text_similarity` edge cases
- Test `collect_interactable_elements` with a simple HTML page (Playwright fixture)
- Test `detect_captcha_challenge` with mock page containing reCAPTCHA iframe
- Test storage state encrypt/decrypt round-trip
- Test `collect_page_content` with structured HTML

## Dependencies

- `playwright` (already in NaumiAgent deps)
- `cryptography` (need to add to pyproject.toml)

## Checklist

- [ ] `som.py` created with all 11 functions
- [ ] Unit tests passing
- [ ] `ruff check` clean
- [ ] Storage state encryption works end-to-end
- [ ] SoM overlay injection + screenshot visual verification
