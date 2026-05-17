"""Set-of-Mark (SoM) visual interaction helpers and shared utilities.

Ported from browser-debugging-daemon/scripts/shared.js.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import platform
import tempfile
from pathlib import Path
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# --- Interactive selectors (same as JS source) ---
INTERACTIVE_SELECTORS = ", ".join([
    "a",
    "button",
    "input",
    "textarea",
    "select",
    "details",
    '[role="button"]',
    '[role="link"]',
    '[role="menuitem"]',
    '[role="tab"]',
    '[role="option"]',
    '[role="switch"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="card"]',
    '[role="dialog"]',
    '[role="treeitem"]',
    '[contenteditable=""]',
    '[contenteditable="true"]',
    "[onclick]",
    "[tabindex]",
    "[data-testid]",
    "[data-action]",
    "[data-clickable]",
    "[draggable='true']",
])

# --- CAPTCHA signatures ---
_CAPTCHA_SIGNATURES: list[dict[str, str]] = [
    {"selector": 'iframe[src*="recaptcha"]', "type": "recaptcha", "label": "reCAPTCHA"},
    {"selector": 'iframe[src*="hcaptcha"]', "type": "hcaptcha", "label": "hCaptcha"},
    {
        "selector": 'iframe[src*="challenges.cloudflare"]',
        "type": "turnstile",
        "label": "Cloudflare Turnstile",
    },
    {"selector": 'iframe[src*="captcha"]', "type": "unknown", "label": "CAPTCHA iframe"},
    {"selector": ".g-recaptcha", "type": "recaptcha", "label": "reCAPTCHA widget"},
    {"selector": ".h-captcha", "type": "hcaptcha", "label": "hCaptcha widget"},
    {"selector": "[data-sitekey]", "type": "unknown", "label": "CAPTCHA with sitekey"},
    {"selector": "#challenge-running", "type": "cloudflare", "label": "Cloudflare challenge"},
    {"selector": "#challenge-stage", "type": "cloudflare", "label": "Cloudflare challenge stage"},
    {"selector": ".challenge-platform", "type": "cloudflare", "label": "Cloudflare platform"},
    {"selector": '[id*="captcha"]', "type": "unknown", "label": "CAPTCHA element"},
    {"selector": '[class*="captcha"]', "type": "unknown", "label": "CAPTCHA class"},
]

_CAPTCHA_TEXT_SIGNALS: list[dict[str, str]] = [
    {"text": "i'm not a robot", "type": "recaptcha"},
    {"text": "verify you are human", "type": "cloudflare"},
    {"text": "are you a robot", "type": "unknown"},
    {"text": "prove you are human", "type": "unknown"},
    {"text": "press and hold", "type": "funcaptcha"},
    {"text": "select all images", "type": "recaptcha"},
    {"text": "click on the", "type": "unknown"},
]


# ---------------------------------------------------------------------------
# Text similarity & element scoring
# ---------------------------------------------------------------------------

def _normalize_value(value: Any) -> str:
    return str(value).strip().lower() if isinstance(value, str) else ""


def text_similarity(left: Any, right: Any) -> float:
    a = _normalize_value(left)
    b = _normalize_value(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.6
    return 0.0


def score_candidate(previous: dict[str, Any], candidate: dict[str, Any]) -> float:
    score = 0.0
    if previous.get("tag") == candidate.get("tag"):
        score += 40
    if previous.get("role") and previous.get("role") == candidate.get("role"):
        score += 15
    if previous.get("type") and previous.get("type") == candidate.get("type"):
        score += 15
    if previous.get("href") and previous.get("href") == candidate.get("href"):
        score += 20

    score += text_similarity(previous.get("text"), candidate.get("text")) * 60
    score += text_similarity(previous.get("placeholder"), candidate.get("placeholder")) * 30
    score += text_similarity(previous.get("ariaLabel"), candidate.get("ariaLabel")) * 30
    score += text_similarity(previous.get("name"), candidate.get("name")) * 15
    score += text_similarity(previous.get("title"), candidate.get("title")) * 15

    px = previous.get("x", 0)
    py = previous.get("y", 0)
    cx = candidate.get("x", 0)
    cy = candidate.get("y", 0)
    distance = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
    score -= min(distance / 25, 20)

    return score


# ---------------------------------------------------------------------------
# Page interaction: clear overlays
# ---------------------------------------------------------------------------

async def clear_overlays(page: Page) -> None:
    await page.evaluate(
        """() => {
            document.querySelectorAll('.agent-som-overlay').forEach(el => el.remove());
        }"""
    )


# ---------------------------------------------------------------------------
# Collect interactable elements (SoM)
# ---------------------------------------------------------------------------

_COLLECT_ELEMENTS_JS = """
async ({ drawOverlays, interactiveSelectors }) => {
    document.querySelectorAll('.agent-som-overlay').forEach(el => el.remove());
    document.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));

    let idCounter = 1;
    const elements = [];

    const getLabel = (element) => {
        const visibleText = element.innerText || element.textContent || "";
        const ariaLabel = element.getAttribute('aria-label') || "";
        const title = element.getAttribute('title') || "";
        const dataTestId = element.getAttribute('data-testid') || "";
        const placeholder = element.getAttribute('placeholder') || "";
        const value = typeof element.value === 'string' ? element.value : "";
        const label = (
            visibleText || ariaLabel || title || dataTestId || placeholder || value
        );
        return label.trim().slice(0, 80);
    };

    const getSurroundingHtml = (element) => {
        const outer = element.outerHTML || "";
        const truncated = outer.length > 300 ? outer.slice(0, 300) + "..." : outer;
        const p = element.parentElement;
        const pTag = p ? p.tagName.toLowerCase() : "";
        const pId = p && p.id ? "#" + p.id : "";
        const pCls = p && p.className && typeof p.className === 'string'
            ? "." + p.className.trim().split(/\\s+/).slice(0, 2).join(".")
            : "";
        return {
            html: truncated,
            parent: pTag ? pTag + pId + pCls : null,
        };
    };

    document.querySelectorAll(interactiveSelectors).forEach((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        const isVisible =
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none' &&
            style.opacity !== '0';

        if (!isVisible) return;

        const id = idCounter++;
        const entry = {
            id,
            tag: element.tagName.toLowerCase(),
            text: getLabel(element),
            contextHtml: getSurroundingHtml(element),
            ariaLabel: element.getAttribute('aria-label') || "",
            name: element.getAttribute('name') || "",
            role: element.getAttribute('role') || "",
            type: element.getAttribute('type') || "",
            title: element.getAttribute('title') || "",
            href: element.getAttribute('href') || "",
            dataTestId: element.getAttribute('data-testid') || "",
            placeholder: element.getAttribute('placeholder') || "",
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2,
        };

        elements.push(entry);
        element.setAttribute('data-som-id', String(id));

        if (!drawOverlays) return;

        const overlay = document.createElement('div');
        overlay.className = 'agent-som-overlay';
        overlay.style.position = 'fixed';
        overlay.style.top = Math.max(0, rect.top - 10) + 'px';
        overlay.style.left = Math.max(0, rect.left - 10) + 'px';
        overlay.style.backgroundColor = 'rgba(255, 0, 0, 0.85)';
        overlay.style.color = 'white';
        overlay.style.fontSize = '12px';
        overlay.style.fontWeight = 'bold';
        overlay.style.padding = '2px 4px';
        overlay.style.borderRadius = '4px';
        overlay.style.border = '1px solid white';
        overlay.style.zIndex = '2147483647';
        overlay.style.pointerEvents = 'none';
        overlay.innerText = String(id);
        document.body.appendChild(overlay);
    });

    return elements;
}
"""


async def collect_interactable_elements(
    page: Page,
    *,
    draw_overlays: bool = False,
) -> list[dict[str, Any]]:
    result = await page.evaluate(
        _COLLECT_ELEMENTS_JS,
        {"drawOverlays": draw_overlays, "interactiveSelectors": INTERACTIVE_SELECTORS},
    )
    return result or []


# ---------------------------------------------------------------------------
# Refresh target (re-score after page change)
# ---------------------------------------------------------------------------

async def refresh_target(
    page: Page,
    observed_elements: list[dict[str, Any]],
    element_id: int,
) -> dict[str, Any]:
    previous = next((e for e in observed_elements if e.get("id") == element_id), None)
    if previous is None:
        raise ValueError(f"Element {element_id} not found in last observation.")

    live_elements = await collect_interactable_elements(page)
    if not live_elements:
        return previous

    ranked = sorted(
        [{"candidate": c, "score": score_candidate(previous, c)} for c in live_elements],
        key=lambda x: x["score"],
        reverse=True,
    )

    best = ranked[0] if ranked else None
    if not best or best["score"] < 35:
        return previous

    return {**previous, **best["candidate"]}


# ---------------------------------------------------------------------------
# Collect page content (headings, paragraphs, lists, tables)
# ---------------------------------------------------------------------------

_COLLECT_PAGE_CONTENT_JS = """
() => {
    const result = { headings: [], paragraphs: [], lists: [], tables: [] };

    document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach((el) => {
        const text = (el.innerText || '').trim();
        if (text) {
            result.headings.push({
                level: parseInt(el.tagName[1], 10),
                text: text.slice(0, 200),
            });
        }
    });

    document.querySelectorAll('p').forEach((el, i) => {
        if (i >= 10) return;
        const text = (el.innerText || '').trim();
        if (text && text.length > 5) {
            result.paragraphs.push(text.slice(0, 300));
        }
    });

    document.querySelectorAll('ul,ol').forEach((el, i) => {
        if (i >= 5) return;
        const items = Array.from(el.querySelectorAll('li'))
            .slice(0, 8)
            .map(li => (li.innerText || '').trim().slice(0, 100));
        if (items.length > 0) {
            result.lists.push(items);
        }
    });

    document.querySelectorAll('table').forEach((table, i) => {
        if (i >= 3) return;
        const rows = Array.from(table.querySelectorAll('tr'))
            .slice(0, 10)
            .map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                    .map(cell => (cell.innerText || '').trim().slice(0, 80))
            );
        if (rows.length > 0) {
            result.tables.push(rows);
        }
    });

    if (result.headings.length === 0) delete result.headings;
    if (result.paragraphs.length === 0) delete result.paragraphs;
    if (result.lists.length === 0) delete result.lists;
    if (result.tables.length === 0) delete result.tables;

    return result;
}
"""


async def collect_page_content(page: Page) -> dict[str, Any]:
    return await page.evaluate(_COLLECT_PAGE_CONTENT_JS) or {}


# ---------------------------------------------------------------------------
# CAPTCHA detection
# ---------------------------------------------------------------------------

_DETECT_CAPTCHA_JS = """
(signatures) => {
    const found = [];
    for (const sig of signatures) {
        const elements = document.querySelectorAll(sig.selector);
        if (elements.length > 0) {
            const el = elements[0];
            const rect = el.getBoundingClientRect();
            const visible = rect.width > 0 && rect.height > 0;
            found.push({
                type: sig.type,
                label: sig.label,
                selector: sig.selector,
                visible,
                count: elements.length,
            });
        }
    }

    const bodyText = (document.body?.innerText || '').toLowerCase().slice(0, 2000);
    const textSignals = [
        { text: "i'm not a robot", type: "recaptcha" },
        { text: "verify you are human", type: "cloudflare" },
        { text: "are you a robot", type: "unknown" },
        { text: "prove you are human", type: "unknown" },
        { text: "press and hold", type: "funcaptcha" },
        { text: "select all images", type: "recaptcha" },
        { text: "click on the", type: "unknown" },
    ];
    for (const signal of textSignals) {
        if (bodyText.includes(signal.text)) {
            found.push({
                type: signal.type,
                label: 'Text: "' + signal.text + '"',
                selector: null,
                visible: true,
                count: 1,
            });
            break;
        }
    }

    return found.length > 0 ? found : null;
}
"""


async def detect_captcha_challenge(page: Page) -> list[dict[str, Any]] | None:
    signatures = [
        {"selector": s["selector"], "type": s["type"], "label": s["label"]}
        for s in _CAPTCHA_SIGNATURES
    ]
    return await page.evaluate(_DETECT_CAPTCHA_JS, signatures)


# ---------------------------------------------------------------------------
# Storage state encryption (AES-256-GCM)
# ---------------------------------------------------------------------------

def _get_storage_state_secret() -> str:
    val = os.environ.get("BROWSER_STORAGE_STATE_SECRET", "")
    return val.strip() if isinstance(val, str) else ""


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _encrypt_storage_state(payload_json: str, secret: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = os.urandom(12)
    key = _derive_key(secret)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, payload_json.encode("utf-8"), None)
    return {
        "__encrypted": True,
        "v": 1,
        "alg": "aes-256-gcm",
        "iv": base64.b64encode(iv).decode("ascii"),
        "tag": base64.b64encode(ciphertext[-16:]).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext[:-16]).decode("ascii"),
    }


def _decrypt_storage_state(encrypted: dict[str, Any], secret: str) -> dict[str, Any]:
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive_key(secret)
    iv = base64.b64decode(encrypted["iv"])
    tag = base64.b64decode(encrypted["tag"])
    ciphertext = base64.b64decode(encrypted["ciphertext"])

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return json.loads(plaintext.decode("utf-8"))


def _tighten_permissions(path: str | Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


async def load_storage_state(storage_state_path: str | Path) -> dict[str, Any] | None:
    import json

    path = Path(storage_state_path)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text("utf-8"))
        if data.get("__encrypted"):
            secret = _get_storage_state_secret()
            if not secret:
                raise ValueError(
                    "BROWSER_STORAGE_STATE_SECRET required to decrypt"
                    " storage_state.json"
                )
            return _decrypt_storage_state(data, secret)
        return data
    except Exception:
        logger.exception("Failed to load storage state from %s", storage_state_path)
        return None


async def save_browser_state(context: Any, storage_state_path: str | Path) -> bool:
    import json

    if context is None:
        return False

    try:
        state = await context.storage_state()
        secret = _get_storage_state_secret()
        payload: Any = _encrypt_storage_state(json.dumps(state), secret) if secret else state
        Path(storage_state_path).write_text(
            json.dumps(payload, indent=2),
            "utf-8",
        )
        _tighten_permissions(storage_state_path)
        logger.info("Browser session saved (cookies, localStorage)")
        return True
    except Exception:
        logger.exception("Failed to save browser state")
        return False


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def write_base64_files(files: list[dict[str, str]]) -> list[str]:
    if not files:
        return []

    tmp_dir = tempfile.mkdtemp(prefix="browser-daemon-upload-")
    written: list[str] = []

    for f in files:
        name = f.get("name")
        content = f.get("content")
        if not name or not isinstance(content, str):
            raise ValueError("Each file must have 'name' (string) and 'content' (base64 string).")

        target = os.path.join(tmp_dir, os.path.basename(name))
        with open(target, "wb") as fh:
            fh.write(base64.b64decode(content))
        _tighten_permissions(target)
        written.append(target)

    return written


def get_select_all_shortcut() -> str:
    return "Meta+A" if platform.system() == "Darwin" else "Control+A"
