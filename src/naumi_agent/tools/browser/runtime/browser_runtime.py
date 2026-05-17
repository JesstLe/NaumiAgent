"""Core browser runtime: lifecycle, actions, visual feedback, screencast.

Ported from browser-debugging-daemon/scripts/runtime/BrowserRuntime.js (2565 lines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from ..som import (
    clear_overlays,
    collect_interactable_elements,
    collect_page_content,
    detect_captcha_challenge,
    load_storage_state,
    refresh_target,
    save_browser_state,
    write_base64_files,
)
from .artifact_store import ArtifactStore
from .chrome_launcher import ChromeLauncher
from .download_manager import DownloadManager
from .network_recorder import NetworkRecorder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSS — Action highlight (beam / bloom / fallback / action-bar)
# ---------------------------------------------------------------------------

ACTION_HIGHLIGHT_STYLES = """\
@property --agent-beam-angle {
    syntax: "<angle>";
    initial-value: 0deg;
    inherits: true;
}
@property --agent-beam-opacity {
    syntax: "<number>";
    initial-value: 0;
    inherits: true;
}

@keyframes agent-beam-spin {
    to { --agent-beam-angle: 360deg; }
}
@keyframes agent-beam-fade-in {
    to { --agent-beam-opacity: 1; }
}
@keyframes agent-beam-fade-out {
    from { --agent-beam-opacity: 1; }
    to { --agent-beam-opacity: 0; }
}
@keyframes agent-beam-hue {
    0%   { filter: hue-rotate(-30deg) brightness(1.3) saturate(1.2); }
    50%  { filter: hue-rotate(30deg) brightness(1.3) saturate(1.2); }
    100% { filter: hue-rotate(-30deg) brightness(1.3) saturate(1.2); }
}

/* ── Beam wrapper ── */
.agent-beam-wrap {
    position: relative;
    border-radius: inherit;
}
.agent-beam-wrap.agent-beam-active {
    animation:
        agent-beam-spin 2s linear infinite,
        agent-beam-fade-in 0.35s ease forwards;
}
.agent-beam-wrap.agent-beam-fading {
    animation:
        agent-beam-spin 2s linear infinite,
        agent-beam-fade-out 0.35s ease forwards;
}

/* ── Beam stroke (rotating conic gradient masked to border) ── */
.agent-beam-wrap.agent-beam-active::after,
.agent-beam-wrap.agent-beam-fading::after {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    padding: 4px;
    clip-path: inset(0 round var(--beam-br, 4px));
    background:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 54%,
            rgba(255,255,255,0.1) 57%,
            rgba(255,255,255,0.3) 60%,
            rgba(255,255,255,0.6) 63%,
            rgba(255,255,255,0.75) 66%,
            rgba(255,255,255,0.6) 69%,
            rgba(255,255,255,0.3) 72%,
            rgba(255,255,255,0.1) 75%,
            transparent 78%, transparent 100%
        ),
        radial-gradient(ellipse 70px 40px at 33% -7.4%, rgb(255,50,100), transparent),
        radial-gradient(ellipse 60px 35px at 12% -5%, rgb(40,140,255), transparent),
        radial-gradient(ellipse 40px 70px at 2.1% 68.3%, rgb(50,200,80), transparent),
        radial-gradient(ellipse 180px 32px at 74.4% 100%, rgb(100,70,255), transparent),
        radial-gradient(ellipse 85px 26px at 55% 100%, rgb(40,140,255), transparent),
        radial-gradient(ellipse 74px 32px at 93.9% 0%, rgb(255,120,40), transparent),
        radial-gradient(ellipse 26px 42px at 100% 27.1%, rgb(240,50,180), transparent),
        radial-gradient(ellipse 52px 48px at 100% 27.1%, rgb(180,40,240), transparent);
    -webkit-mask:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(#fff 0 0) content-box,
        linear-gradient(#fff 0 0);
    -webkit-mask-composite: source-in, xor;
    mask:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(#fff 0 0) content-box,
        linear-gradient(#fff 0 0);
    mask-composite: intersect, exclude;
    pointer-events: none;
    z-index: 2147483646;
    opacity: calc(var(--agent-beam-opacity) * 0.8);
    animation: agent-beam-hue 12s ease-in-out infinite;
}

/* ── Inner glow ── */
.agent-beam-wrap.agent-beam-active::before,
.agent-beam-wrap.agent-beam-fading::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background:
        radial-gradient(ellipse 63px 36px at 33% -7.4%, rgba(255,50,100,0.45), transparent),
        radial-gradient(ellipse 54px 32px at 12% -5%, rgba(40,140,255,0.45), transparent),
        radial-gradient(ellipse 36px 63px at 2.1% 68.3%, rgba(50,200,80,0.45), transparent),
        radial-gradient(ellipse 162px 29px at 74.4% 100%, rgba(100,70,255,0.45), transparent),
        radial-gradient(ellipse 77px 23px at 55% 100%, rgba(40,140,255,0.45), transparent),
        radial-gradient(ellipse 67px 29px at 93.9% 0%, rgba(255,120,40,0.35), transparent);
    box-shadow: inset 0 0 9px 1px rgba(255,255,255,0.27);
    -webkit-mask-image:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
        linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
    -webkit-mask-composite: source-in, source-over;
    mask-image:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
        linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
    mask-composite: intersect, add;
    pointer-events: none;
    z-index: 2147483645;
    opacity: calc(var(--agent-beam-opacity) * 1.0);
    clip-path: inset(0 round var(--beam-br, 4px));
    animation: agent-beam-hue 12s ease-in-out infinite;
}

/* ── Outer bloom (blurred glow) ── */
.agent-beam-bloom {
    display: none;
    position: absolute;
    inset: 0;
    border-radius: inherit;
    padding: 4px;
    clip-path: inset(0 round var(--beam-br, 4px));
    background:
        conic-gradient(
            from var(--agent-beam-angle),
            transparent 0%, transparent 58%,
            rgba(255,255,255,0.03) 62%,
            rgba(255,255,255,0.08) 65%,
            rgba(255,255,255,0.2) 67%,
            rgba(255,255,255,0.45) 69%,
            rgba(255,255,255,0.85) 70%,
            rgba(255,255,255,0.85) 70.5%,
            rgba(255,255,255,0.45) 71.5%,
            rgba(255,255,255,0.2) 73%,
            rgba(255,255,255,0.08) 75%,
            rgba(255,255,255,0.03) 78%,
            transparent 82%
        );
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor;
    mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    mask-composite: exclude;
    filter: blur(8px) brightness(1.3) saturate(1.2);
    pointer-events: none;
    z-index: 2147483644;
    opacity: 0;
}
.agent-beam-wrap.agent-beam-active .agent-beam-bloom,
.agent-beam-wrap.agent-beam-fading .agent-beam-bloom {
    display: block;
    opacity: calc(var(--agent-beam-opacity) * 0.8);
}

/* ── Fallback for browsers without @property ── */
.agent-highlight-target {
    outline: 3px solid rgba(99,102,241,0.9) !important;
    outline-offset: 3px !important;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.3), 0 0 20px rgba(99,102,241,0.2) !important;
    transition: outline 0.15s ease, box-shadow 0.15s ease !important;
}

/* ── Action bar ── */
#agent-action-bar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; gap: 8px;
    padding: 6px 14px; box-sizing: border-box;
    background: rgba(15,23,42,0.92); color: #e2e8f0; font-size: 13px;
    border-bottom: 1px solid rgba(99,102,241,0.4);
    pointer-events: none; user-select: none;
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
#agent-action-bar .step { color: #818cf8; font-weight: 600; white-space: nowrap; }
#agent-action-bar .action { color: #f8fafc; }
#agent-action-bar .detail {
    color: #94a3b8; max-width: 420px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
}
"""

# ---------------------------------------------------------------------------
# CSS — Browser active border (frame / bloom / badge)
# ---------------------------------------------------------------------------

BROWSER_ACTIVE_BORDER_STYLES = """\
@property --agent-frame-angle {
    syntax: "<angle>";
    initial-value: 0deg;
    inherits: false;
}
@property --agent-frame-opacity {
    syntax: "<number>";
    initial-value: 0;
    inherits: false;
}

@keyframes agent-frame-spin {
    to { --agent-frame-angle: 360deg; }
}
@keyframes agent-frame-fade-in {
    to { --agent-frame-opacity: 1; }
}
@keyframes agent-frame-hue {
    0%   { filter: hue-rotate(-30deg) brightness(1.2) saturate(1.15); }
    50%  { filter: hue-rotate(30deg) brightness(1.2) saturate(1.15); }
    100% { filter: hue-rotate(-30deg) brightness(1.2) saturate(1.15); }
}

/* ── Frame wrapper ── */
#agent-browser-active-border {
    position: fixed;
    inset: 0;
    border-radius: 0;
    z-index: 2147483643;
    pointer-events: none;
    user-select: none;
    animation: agent-frame-spin 3s linear infinite, agent-frame-fade-in 0.5s ease forwards;
}

/* ── Beam stroke ── */
#agent-browser-active-border::after {
    content: "";
    position: absolute;
    inset: 0;
    padding: 3px;
    clip-path: inset(0);
    background:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 54%,
            rgba(255,255,255,0.1) 57%,
            rgba(255,255,255,0.3) 60%,
            rgba(255,255,255,0.6) 63%,
            rgba(255,255,255,0.75) 66%,
            rgba(255,255,255,0.6) 69%,
            rgba(255,255,255,0.3) 72%,
            rgba(255,255,255,0.1) 75%,
            transparent 78%, transparent 100%
        ),
        radial-gradient(ellipse 90px 50px at 33% -7.4%, rgb(255,50,100), transparent),
        radial-gradient(ellipse 75px 44px at 12% -5%, rgb(40,140,255), transparent),
        radial-gradient(ellipse 50px 85px at 2.1% 68.3%, rgb(50,200,80), transparent),
        radial-gradient(ellipse 220px 40px at 74.4% 100%, rgb(100,70,255), transparent),
        radial-gradient(ellipse 100px 32px at 55% 100%, rgb(40,140,255), transparent),
        radial-gradient(ellipse 90px 40px at 93.9% 0%, rgb(255,120,40), transparent),
        radial-gradient(ellipse 32px 52px at 100% 27.1%, rgb(240,50,180), transparent),
        radial-gradient(ellipse 65px 60px at 100% 27.1%, rgb(180,40,240), transparent);
    -webkit-mask:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(#fff 0 0) content-box,
        linear-gradient(#fff 0 0);
    -webkit-mask-composite: source-in, xor;
    mask:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(#fff 0 0) content-box,
        linear-gradient(#fff 0 0);
    mask-composite: intersect, exclude;
    pointer-events: none;
    z-index: 2147483646;
    opacity: calc(var(--agent-frame-opacity) * 0.7);
    animation: agent-frame-hue 14s ease-in-out infinite;
}

/* ── Inner glow ── */
#agent-browser-active-border::before {
    content: "";
    position: absolute;
    inset: 0;
    background:
        radial-gradient(ellipse 80px 45px at 33% -7.4%, rgba(255,50,100,0.4), transparent),
        radial-gradient(ellipse 68px 40px at 12% -5%, rgba(40,140,255,0.4), transparent),
        radial-gradient(ellipse 45px 80px at 2.1% 68.3%, rgba(50,200,80,0.4), transparent),
        radial-gradient(ellipse 200px 36px at 74.4% 100%, rgba(100,70,255,0.4), transparent),
        radial-gradient(ellipse 96px 28px at 55% 100%, rgba(40,140,255,0.4), transparent),
        radial-gradient(ellipse 84px 36px at 93.9% 0%, rgba(255,120,40,0.35), transparent);
    box-shadow: inset 0 0 12px 2px rgba(255,255,255,0.2);
    -webkit-mask-image:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(white, transparent 32px, transparent calc(100% - 32px), white),
        linear-gradient(to right, white, transparent 32px, transparent calc(100% - 32px), white);
    -webkit-mask-composite: source-in, source-over;
    mask-image:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 30%,
            rgba(255,255,255,0.1) 36%, rgba(255,255,255,0.35) 44%,
            white 52%, white 80%,
            rgba(255,255,255,0.35) 86%, rgba(255,255,255,0.1) 92%,
            transparent 95%, transparent 100%
        ),
        linear-gradient(white, transparent 32px, transparent calc(100% - 32px), white),
        linear-gradient(to right, white, transparent 32px, transparent calc(100% - 32px), white);
    mask-composite: intersect, add;
    pointer-events: none;
    z-index: 2147483645;
    opacity: calc(var(--agent-frame-opacity) * 0.9);
    animation: agent-frame-hue 14s ease-in-out infinite;
}

/* ── Bloom (blurred glow) ── */
#agent-browser-active-bloom {
    position: fixed;
    inset: 0;
    padding: 3px;
    clip-path: inset(0);
    background:
        conic-gradient(
            from var(--agent-frame-angle),
            transparent 0%, transparent 58%,
            rgba(255,255,255,0.03) 62%,
            rgba(255,255,255,0.08) 65%,
            rgba(255,255,255,0.2) 67%,
            rgba(255,255,255,0.45) 69%,
            rgba(255,255,255,0.85) 70%,
            rgba(255,255,255,0.85) 70.5%,
            rgba(255,255,255,0.45) 71.5%,
            rgba(255,255,255,0.2) 73%,
            rgba(255,255,255,0.08) 75%,
            rgba(255,255,255,0.03) 78%,
            transparent 82%
        );
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor;
    mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    mask-composite: exclude;
    filter: blur(10px) brightness(1.3) saturate(1.2);
    pointer-events: none;
    z-index: 2147483644;
    opacity: calc(var(--agent-frame-opacity) * 0.6);
}

/* ── Badge ── */
#agent-browser-active-badge {
    position: fixed;
    bottom: 14px;
    right: 14px;
    z-index: 2147483647;
    pointer-events: none;
    user-select: none;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 11px;
    font-weight: 600;
    color: #e0e7ff;
    background: rgba(15,23,42,0.85);
    border: 1px solid rgba(99,102,241,0.45);
    border-radius: 6px;
    padding: 4px 10px;
    letter-spacing: 0.3px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    gap: 6px;
    box-shadow: 0 0 12px rgba(99,102,241,0.25);
}
#agent-browser-active-badge .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #818cf8;
    box-shadow: 0 0 6px #818cf8;
    animation: agent-frame-fade-in 2.5s ease-in-out infinite alternate;
}
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_BROWSER_SOURCES = frozenset(["auto", "managed", "attached"])

DEFAULT_TEXT_AUDIT_SELECTORS = ", ".join([
    "button",
    "a",
    "label",
    "input[type='button']",
    "input[type='submit']",
    "input[type='reset']",
    "[role='button']",
    "[role='link']",
    "[data-testid]",
])

ATTACHED_SCREENCAST_FPS = 10
ATTACHED_SCREENCAST_MAX_PENDING_WRITES = int(
    os.environ.get(
        "BROWSER_ATTACHED_SCREENCAST_MAX_PENDING_WRITES", "80"
    )
)

_LOG_BUFFER_LIMIT = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trim_log_buffer(buffer: list[Any], limit: int = _LOG_BUFFER_LIMIT) -> list[Any]:
    if len(buffer) <= limit:
        return buffer
    return buffer[len(buffer) - limit:]


def _normalize_browser_source(source: str | None) -> str:
    candidate = (source or "").strip().lower()
    if candidate in SUPPORTED_BROWSER_SOURCES:
        return candidate
    return "auto"


async def _is_cdp_endpoint(endpoint: str) -> bool:
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{endpoint}/json/version",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                if resp.status != 200:
                    return False
                body = await resp.json()
                return isinstance(body.get("webSocketDebuggerUrl"), str)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# BrowserRuntime
# ---------------------------------------------------------------------------


class BrowserRuntime:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.storage_state_path = self.base_dir / "storage_state.json"
        self.artifacts = ArtifactStore(base_dir)
        self.last_session_summary: dict[str, Any] | None = None
        self.requested_source: str = "auto"
        self.session_source: str = "managed"
        self.cdp_endpoint: str = os.environ.get(
            "CHROME_REMOTE_DEBUG_URL", "http://127.0.0.1:9222"
        )

        cdp_port = 9222
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self.cdp_endpoint)
            cdp_port = parsed.port or 9222
        except Exception:
            pass

        self.chrome_launcher = ChromeLauncher(cdp_port=cdp_port)
        self.launched_chrome: bool = False
        self.auto_fallback_reason: str | None = None
        self.browser_mode: str = "stopped"
        self.manual_control_active: bool = False
        self.trace_active: bool = False
        self.attached_video_capability: bool = False
        self.attached_screencast: dict[str, Any] | None = None
        self.ffmpeg_available: bool | None = None
        self.network_recorder = NetworkRecorder()
        self.download_manager = DownloadManager(self.base_dir / "artifacts")
        self._playwright: Any = None
        self.reset_runtime_state()

    # ── State management ──

    def reset_runtime_state(self) -> None:
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.interactable_elements: list[dict[str, Any]] = []
        self.console_messages: list[dict[str, Any]] = []
        self.network_events: list[dict[str, Any]] = []
        self.page_errors: list[dict[str, Any]] = []
        self.requested_source = "auto"
        self.session_source = "managed"
        self.auto_fallback_reason = None
        self.browser_mode = "stopped"
        self.launched_chrome = False
        self.manual_control_active = False
        self.trace_active = False
        self.attached_video_capability = False
        self.attached_screencast = None

    def is_running(self) -> bool:
        return self.context is not None

    def current_session_source(self) -> str | None:
        return self.session_source if self.context else None

    async def ensure_started(self, options: dict[str, Any] | None = None) -> None:
        if not self.browser:
            await self.start(options or {})

    # ── Lifecycle: start ──

    async def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if self.browser:
            return {
                "alreadyRunning": True,
                "artifacts": self.artifacts.get_summary(),
                "requestedSource": self.requested_source,
                "browserMode": self.browser_mode,
                "sessionSource": self.session_source,
                "autoFallbackReason": self.auto_fallback_reason,
            }

        self.artifacts.start_session()
        self.requested_source = _normalize_browser_source(
            options.get("source", "auto")
        )
        self.session_source = self.requested_source
        self.cdp_endpoint = options.get("cdpEndpoint", self.cdp_endpoint)
        self.auto_fallback_reason = None

        if self.requested_source == "managed":
            await self._launch_browser_session(
                headless=options.get("headless", True),
                event_type="session_started",
            )
        else:
            is_attached_mode = self.requested_source == "attached"
            attached = False

            # Phase 1: try attaching to existing CDP endpoint
            if await _is_cdp_endpoint(self.cdp_endpoint):
                try:
                    await self._attach_browser_session(
                        endpoint=self.cdp_endpoint,
                        event_type=(
                            "session_attached"
                            if is_attached_mode
                            else "session_auto_attached"
                        ),
                    )
                    attached = True
                except Exception as attach_exc:
                    self.auto_fallback_reason = str(attach_exc)
                    logger.error("CDP attach failed: %s", attach_exc)
                    await self._cleanup_broken_session()
            else:
                import re

                port_match = re.search(r":(\d+)", self.cdp_endpoint)
                port_hint = port_match.group(1) if port_match else "9222"
                self.auto_fallback_reason = (
                    f"No CDP server at {self.cdp_endpoint}"
                )
                logger.warning(
                    "No CDP endpoint at %s. Launch Chrome with "
                    "--remote-debugging-port=%s for visible browser.",
                    self.cdp_endpoint,
                    port_hint,
                )
                self.artifacts.append_event(
                    "session_cdp_unavailable",
                    {
                        "requestedSource": self.requested_source,
                        "endpoint": self.cdp_endpoint,
                    },
                )

            # Phase 2: launch Chrome via ChromeLauncher and attach
            if not attached:
                try:
                    readiness = await self.chrome_launcher.ensure_ready(
                        force_resync=options.get("forceResync", False),
                    )
                    if readiness.get("launched"):
                        self.launched_chrome = True
                        self.cdp_endpoint = readiness["endpoint"]
                    self.artifacts.append_event(
                        "session_chrome_auto_launched",
                        {
                            "launched": readiness.get("launched"),
                            "synced": readiness.get("synced"),
                            "port": readiness.get("port"),
                        },
                    )
                    await self._attach_browser_session(
                        endpoint=self.cdp_endpoint,
                        event_type=(
                            "session_attached"
                            if is_attached_mode
                            else "session_auto_attached"
                        ),
                    )
                except Exception as launch_or_attach_exc:
                    await self._cleanup_broken_session()
                    self.chrome_launcher.kill_chrome()
                    self.launched_chrome = False
                    if self.auto_fallback_reason:
                        self.auto_fallback_reason = (
                            f"{self.auto_fallback_reason}; "
                            f"{launch_or_attach_exc}"
                        )
                    else:
                        self.auto_fallback_reason = str(launch_or_attach_exc)

                    if is_attached_mode:
                        raise RuntimeError(
                            "Cannot attach to Chrome and auto-launch failed: "
                            f"{launch_or_attach_exc}"
                        ) from launch_or_attach_exc

                    # auto mode: fall back to managed headful then headless
                    self.artifacts.append_event(
                        "session_auto_launch_failed",
                        {"error": str(launch_or_attach_exc)},
                    )
                    logger.warning(
                        "CDP unavailable (%s). ChromeLauncher also failed (%s). "
                        "Falling back to managed mode.",
                        self.auto_fallback_reason,
                        launch_or_attach_exc,
                    )
                    self.session_source = "managed"
                    try:
                        await self._launch_browser_session(
                            headless=False,
                            event_type="session_auto_fallback_headful",
                        )
                    except Exception as headful_exc:
                        self.artifacts.append_event(
                            "session_headful_fallback_failed",
                            {"error": str(headful_exc)},
                        )
                        logger.warning(
                            "Headful launch failed (%s), trying headless.",
                            headful_exc,
                        )
                        await self._launch_browser_session(
                            headless=True,
                            event_type="session_auto_fallback_headless",
                        )

        return {
            "alreadyRunning": False,
            "artifacts": self.artifacts.get_summary(),
            "requestedSource": self.requested_source,
            "browserMode": self.browser_mode,
            "sessionSource": self.session_source,
            "autoFallbackReason": self.auto_fallback_reason,
        }

    # ── Lifecycle: cleanup / stop ──

    async def _cleanup_broken_session(self) -> None:
        try:
            await self._stop_attached_screencast(
                reason="cleanup-broken-session", persist=False
            )
        except Exception:
            pass
        try:
            await self._hide_browser_active_border()
        except Exception:
            pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        self.browser = None
        self.context = None
        self.page = None
        self.interactable_elements = []
        self.browser_mode = "stopped"
        self.manual_control_active = False
        self.trace_active = False
        self.attached_video_capability = False

    async def stop(self) -> dict[str, Any]:
        if not self.browser:
            return {
                "alreadyStopped": True,
                "artifacts": self.last_session_summary,
            }

        self.artifacts.append_event(
            "session_stopping", self.get_debug_state(10)
        )
        self.network_recorder.detach()
        self.download_manager.detach()

        if self.launched_chrome:
            kill_result = self.chrome_launcher.kill_chrome()
            self.launched_chrome = False
            self.artifacts.append_event(
                "chrome_auto_launched_killed",
                {
                    "killed": kill_result.get("killed"),
                    "pid": kill_result.get("pid"),
                },
            )

        await self._stop_attached_screencast(
            reason="session-stop", persist=True
        )
        await save_browser_state(self.context, self.storage_state_path)

        await self._finalize_trace_segment("session-stop")
        await self._hide_browser_active_border()
        if self.browser:
            await self.browser.close()

        self._flush_logs_to_artifacts()

        video_files = self.artifacts.list_video_files()
        if video_files:
            self.artifacts.append_event(
                "session_videos_saved",
                {"count": len(video_files), "files": [str(p) for p in video_files]},
            )

        self.last_session_summary = self.artifacts.get_summary()
        result = {
            "alreadyStopped": False,
            "artifacts": self.last_session_summary,
        }

        self.reset_runtime_state()
        return result

    # ── Managed mode launch ──

    async def _launch_browser_session(
        self,
        *,
        headless: bool = True,
        navigate_to_url: str | None = None,
        event_type: str = "session_started",
    ) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self.browser = await self._playwright.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context_options: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "record_video": {
                "dir": str(self.artifacts.get_video_dir()),
                "size": {"width": 1280, "height": 800},
            },
        }

        storage_state = await load_storage_state(self.storage_state_path)
        if storage_state:
            context_options["storage_state"] = storage_state

        self.context = await self.browser.new_context(**context_options)
        await self.context.tracing.start(
            screenshots=True, snapshots=True, sources=True
        )
        self.trace_active = True
        self.page = await self.context.new_page()
        self._attach_page_observers()
        self.network_recorder.clear()
        self.network_recorder.attach(self.context)
        self.browser_mode = "headless" if headless else "headful"
        self.manual_control_active = not headless
        self.interactable_elements = []

        if navigate_to_url and not navigate_to_url.startswith("about:blank"):
            try:
                await self.page.goto(
                    navigate_to_url,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception:
                pass
            await self._smart_wait()

        self.artifacts.append_event(event_type, {
            "reusedStorageState": storage_state is not None,
            "requestedSource": self.requested_source,
            "sessionSource": self.session_source,
            "browserMode": self.browser_mode,
            "resumedUrl": navigate_to_url,
        })

        await self._install_active_border_init_script()

    # ── Attached mode ──

    async def _attach_browser_session(
        self,
        *,
        endpoint: str,
        event_type: str = "session_attached",
    ) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self.browser = await self._playwright.chromium.connect_over_cdp(
            endpoint
        )
        self.session_source = "attached"
        self.browser_mode = "attached"
        self.manual_control_active = True
        self.interactable_elements = []

        contexts = self.browser.contexts
        self.context = contexts[0] if contexts else None
        if not self.context:
            raise RuntimeError(
                f"No attachable browser context found at {endpoint}. "
                "Open a Chrome tab after enabling remote debugging."
            )

        try:
            await self.context.tracing.start(
                screenshots=True, snapshots=True, sources=True
            )
            self.trace_active = True
        except Exception as exc:
            self.trace_active = False
            self.artifacts.append_event(
                "session_trace_unavailable",
                {
                    "sessionSource": self.session_source,
                    "browserMode": self.browser_mode,
                    "error": str(exc),
                },
            )

        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self._attach_page_observers()
        self.network_recorder.clear()
        self.network_recorder.attach(self.context)
        await self._start_attached_screencast()

        self.artifacts.append_event(event_type, {
            "requestedSource": self.requested_source,
            "sessionSource": self.session_source,
            "browserMode": self.browser_mode,
            "endpoint": endpoint,
            "attachedPageUrl": self.page.url,
        })

        await self._install_active_border_init_script()

    # ── Mode switching ──

    async def switch_browser_mode(
        self,
        target_mode: str,
        reason: str = "mode_switch",
    ) -> dict[str, Any]:
        await self.ensure_started()
        if self.session_source == "attached":
            return {
                "changed": False,
                "browserMode": self.browser_mode,
                "sessionSource": self.session_source,
                "artifacts": self.artifacts.get_summary(),
            }
        if self.browser_mode == target_mode:
            return {
                "changed": False,
                "browserMode": self.browser_mode,
                "sessionSource": self.session_source,
                "artifacts": self.artifacts.get_summary(),
            }

        resume_url = self.page.url if self.page else None
        previous_mode = self.browser_mode
        self.artifacts.append_event("session_switch_requested", {
            "from": previous_mode,
            "to": target_mode,
            "reason": reason,
            "resumeUrl": resume_url,
        })

        await save_browser_state(self.context, self.storage_state_path)
        await self._finalize_trace_segment(f"{reason}-{previous_mode}")
        if self.browser:
            await self.browser.close()
        self.browser = None
        self.context = None
        self.page = None
        self.interactable_elements = []

        try:
            await self._launch_browser_session(
                headless=target_mode != "headful",
                navigate_to_url=resume_url,
                event_type="session_switched",
            )
        except Exception as exc:
            self.artifacts.append_event("session_switch_failed", {
                "from": previous_mode,
                "to": target_mode,
                "reason": reason,
                "error": str(exc),
            })
            try:
                await self._launch_browser_session(
                    headless=previous_mode != "headful",
                    navigate_to_url=resume_url,
                    event_type="session_switch_recovered",
                )
            except Exception as recovery_exc:
                self.artifacts.append_event(
                    "session_switch_recovery_failed",
                    {
                        "from": previous_mode,
                        "to": target_mode,
                        "reason": reason,
                        "error": str(recovery_exc),
                    },
                )
                raise RuntimeError(
                    f"Failed to switch browser mode to {target_mode}: "
                    f"{exc}. Recovery also failed: {recovery_exc}"
                ) from recovery_exc

            raise RuntimeError(
                f"Failed to switch browser mode to {target_mode}: "
                f"{exc}. Restored {previous_mode} mode instead."
            ) from exc

        resumed = self.page.url if self.page else resume_url
        self.artifacts.append_event("session_switch_completed", {
            "from": previous_mode,
            "to": self.browser_mode,
            "reason": reason,
            "resumedUrl": resumed,
        })

        return {
            "changed": True,
            "browserMode": self.browser_mode,
            "sessionSource": self.session_source,
            "resumedUrl": resumed,
            "artifacts": self.artifacts.get_summary(),
        }

    async def enter_manual_control(self) -> dict[str, Any]:
        return await self.switch_browser_mode("headful", "manual_control")

    async def exit_manual_control(self) -> dict[str, Any]:
        return await self.switch_browser_mode("headless", "resume_automation")

    # ── Attached screencast ──

    def _detect_ffmpeg_availability(self) -> bool:
        if isinstance(self.ffmpeg_available, bool):
            return self.ffmpeg_available
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.ffmpeg_available = result.returncode == 0
        except Exception:
            self.ffmpeg_available = False
        return self.ffmpeg_available

    async def _encode_screencast_frames_to_webm(
        self,
        *,
        input_pattern: str,
        output_path: str,
        fps: int,
    ) -> str:
        args = [
            "-y",
            "-framerate",
            str(fps),
            "-i",
            input_pattern,
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuv420p",
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr_data = await proc.communicate()
        if proc.returncode == 0:
            return output_path

        stderr_tail = stderr_data.decode(errors="replace")[-4000:]
        raise RuntimeError(
            f"ffmpeg exited with code {proc.returncode}: {stderr_tail.strip()}"
        )

    async def _start_attached_screencast(self) -> None:
        if (
            self.session_source != "attached"
            or not self.context
            or not self.page
        ):
            return

        recorder_state = self.attached_screencast
        if recorder_state and recorder_state.get("active"):
            return

        if not self._detect_ffmpeg_availability():
            self.attached_video_capability = False
            self.artifacts.append_event(
                "session_attached_video_unavailable",
                {
                    "reason": "ffmpeg_not_found",
                    "hint": (
                        "Install ffmpeg to enable attached-mode video "
                        "replay artifacts."
                    ),
                },
            )
            return

        frames_dir = self.artifacts.get_attached_frames_dir(
            "attached-screencast-frames"
        )
        input_pattern = str(frames_dir / "frame_%06d.jpg")

        cdp_session = None
        on_frame_handler = None

        try:
            cdp_session = await self.context.new_cdp_session(self.page)

            recorder: dict[str, Any] = {
                "active": True,
                "cdp_session": cdp_session,
                "frames_dir": frames_dir,
                "input_pattern": input_pattern,
                "frame_count": 0,
                "received_frames": 0,
                "dropped_frames": 0,
                "started_at": time.time() * 1000,
                "max_pending_writes": ATTACHED_SCREENCAST_MAX_PENDING_WRITES,
                "pending_frame_writes": set(),
                "on_frame": None,
            }

            async def _on_frame(params: dict[str, Any]) -> None:
                if not recorder["active"]:
                    return
                recorder["received_frames"] += 1

                data = params.get("data", "")
                session_id = params.get("sessionId")

                if len(recorder["pending_frame_writes"]) >= recorder[
                    "max_pending_writes"
                ]:
                    recorder["dropped_frames"] += 1
                else:
                    recorder["frame_count"] += 1
                    frame_name = (
                        f"frame_{recorder['frame_count']:06d}.jpg"
                    )
                    frame_path = frames_dir / frame_name

                    async def _write_frame(
                        fp: Path = frame_path,
                        d: str = data,
                    ) -> None:
                        import base64

                        try:
                            fp.write_bytes(base64.b64decode(d))
                        except Exception:
                            recorder["dropped_frames"] += 1
                        finally:
                            recorder["pending_frame_writes"].discard(
                                _write_task
                            )

                    _write_task = asyncio.create_task(_write_frame())
                    recorder["pending_frame_writes"].add(_write_task)

                try:
                    await cdp_session.send(
                        "Page.screencastFrameAck",
                        {"sessionId": session_id},
                    )
                except Exception:
                    pass

            on_frame_handler = _on_frame
            recorder["on_frame"] = _on_frame
            cdp_session.on("Page.screencastFrame", _on_frame)

            await cdp_session.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 75,
                "maxWidth": 1280,
                "maxHeight": 800,
                "everyNthFrame": 1,
            })

            self.attached_screencast = recorder
            self.attached_video_capability = True
            self.artifacts.append_event(
                "session_attached_video_started",
                {
                    "framesDir": str(frames_dir),
                    "fps": ATTACHED_SCREENCAST_FPS,
                    "maxPendingWrites": recorder["max_pending_writes"],
                },
            )
        except Exception as exc:
            if cdp_session and on_frame_handler:
                try:
                    cdp_session.remove_listener(
                        "Page.screencastFrame", on_frame_handler
                    )
                except Exception:
                    pass
            if cdp_session:
                try:
                    await cdp_session.detach()
                except Exception:
                    pass
            self.attached_video_capability = False
            self.artifacts.append_event(
                "session_attached_video_unavailable",
                {
                    "reason": "cdp_start_failed",
                    "error": str(exc),
                },
            )
            if frames_dir.exists():
                shutil.rmtree(frames_dir, ignore_errors=True)

    async def _stop_attached_screencast(
        self,
        *,
        reason: str = "session-stop",
        persist: bool = True,
    ) -> str | None:
        recorder = self.attached_screencast
        if not recorder:
            return None

        self.attached_screencast = None
        recorder["active"] = False

        cdp_session = recorder["cdp_session"]

        try:
            await cdp_session.send("Page.stopScreencast")
        except Exception:
            pass

        on_frame = recorder.get("on_frame")
        if on_frame:
            try:
                cdp_session.remove_listener(
                    "Page.screencastFrame", on_frame
                )
            except Exception:
                pass

        try:
            await cdp_session.detach()
        except Exception:
            pass

        pending = recorder.get("pending_frame_writes", set())
        if pending:
            await asyncio.gather(
                *pending, return_exceptions=True
            )

        frames_dir = recorder["frames_dir"]

        if not persist:
            if frames_dir.exists():
                shutil.rmtree(frames_dir, ignore_errors=True)
            return None

        if recorder["frame_count"] < 2:
            self.artifacts.append_event(
                "session_attached_video_skipped",
                {
                    "reason": "insufficient_frames",
                    "frameCount": recorder["frame_count"],
                    "receivedFrames": recorder["received_frames"],
                    "droppedFrames": recorder["dropped_frames"],
                },
            )
            if frames_dir.exists():
                shutil.rmtree(frames_dir, ignore_errors=True)
            return None

        output_path = str(
            self.artifacts.get_video_path(f"attached-{reason}")
        )
        try:
            await self._encode_screencast_frames_to_webm(
                input_pattern=recorder["input_pattern"],
                output_path=output_path,
                fps=ATTACHED_SCREENCAST_FPS,
            )
        except Exception as exc:
            self.attached_video_capability = False
            self.artifacts.append_event(
                "session_attached_video_failed",
                {
                    "error": str(exc),
                    "frameCount": recorder["frame_count"],
                    "receivedFrames": recorder["received_frames"],
                    "droppedFrames": recorder["dropped_frames"],
                    "framesDir": str(frames_dir),
                },
            )
            return None

        duration_ms = max(
            0, time.time() * 1000 - recorder["started_at"]
        )
        self.artifacts.append_event(
            "session_attached_video_saved",
            {
                "videoPath": output_path,
                "frameCount": recorder["frame_count"],
                "receivedFrames": recorder["received_frames"],
                "droppedFrames": recorder["dropped_frames"],
                "durationMs": duration_ms,
                "fps": ATTACHED_SCREENCAST_FPS,
            },
        )
        if frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)
        return output_path

    # ── Trace segments ──

    async def _finalize_trace_segment(
        self, label: str
    ) -> str | None:
        if not self.context or not self.trace_active:
            return None
        trace_path = self.artifacts.get_trace_path(label)
        await self.context.tracing.stop(path=str(trace_path))
        self.trace_active = False
        return str(trace_path)

    # ── Page observers ──

    def _attach_page_observers(self) -> None:
        if not self.page:
            return
        self.download_manager.attach(self.page)

        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.on("requestfinished", self._on_request_finished)
        self.page.on("requestfailed", self._on_request_failed)

    def _on_console(self, msg: Any) -> None:
        location = msg.location
        loc_dict = None
        if location and location.get("url"):
            loc_dict = {
                "url": location["url"],
                "lineNumber": location.get("lineNumber"),
                "columnNumber": location.get("columnNumber"),
            }
        entry = {
            "type": msg.type,
            "text": msg.text,
            "location": loc_dict,
        }
        self.console_messages = _trim_log_buffer(
            [*self.console_messages, entry]
        )

    def _on_page_error(self, error: Any) -> None:
        entry = {
            "message": str(error),
            "stack": None,
        }
        self.page_errors = _trim_log_buffer(
            [*self.page_errors, entry]
        )

    async def _on_request_finished(self, request: Any) -> None:
        try:
            response = await request.response()
        except Exception:
            response = None

        entry: dict[str, Any] = {
            "type": "requestfinished",
            "url": request.url,
            "method": request.method,
            "resourceType": request.resource_type,
            "status": response.status if response else None,
            "ok": response.ok if response else None,
        }

        if request.method != "GET":
            post_data = request.post_data
            if post_data:
                entry["requestBody"] = (
                    post_data[:4096] + f"... ({len(post_data)} bytes total)"
                    if len(post_data) > 4096
                    else post_data
                )

        capture_types = frozenset(["document", "xhr", "fetch"])
        if response and request.resource_type in capture_types:
            try:
                body = await response.text()
                if body is not None:
                    entry["responseBody"] = (
                        body[:4096]
                        + f"... ({len(body)} bytes total)"
                        if len(body) > 4096
                        else body
                    )
                    entry["responseBodyTruncated"] = len(body) > 4096
            except Exception:
                logger.debug(
                    "Response body not available (redirect or empty)"
                )

        self.network_events = _trim_log_buffer(
            [*self.network_events, entry]
        )

    def _on_request_failed(self, request: Any) -> None:
        failure = request.failure
        failure_text = failure.error_text if failure else "unknown"

        entry: dict[str, Any] = {
            "type": "requestfailed",
            "url": request.url,
            "method": request.method,
            "resourceType": request.resource_type,
            "failure": failure_text,
        }

        if request.method != "GET":
            post_data = request.post_data
            if post_data:
                entry["requestBody"] = (
                    post_data[:4096]
                    + f"... ({len(post_data)} bytes total)"
                    if len(post_data) > 4096
                    else post_data
                )

        self.network_events = _trim_log_buffer(
            [*self.network_events, entry]
        )

    # ── Smart wait ──

    async def _smart_wait(self) -> None:
        if not self.page:
            return
        try:
            await self.page.wait_for_load_state(
                "networkidle", timeout=1500
            )
        except Exception:
            pass
        await self.page.wait_for_timeout(500)

    # ── Screenshot helpers ──

    async def _capture_step_screenshot(
        self, label: str, *, full_page: bool = False
    ) -> str | None:
        if not self.page:
            return None
        screenshot_path = self.artifacts.get_step_screenshot_path(label)
        await self.page.screenshot(path=str(screenshot_path), full_page=full_page)
        return str(screenshot_path)

    # ── Action highlight (beam / bloom / action bar) ──

    async def show_action_highlight(
        self,
        som_id: int | None,
        action_type: str,
        detail: str = "",
        step: int | None = None,
    ) -> None:
        if not self.page:
            return
        try:
            await self.page.evaluate(
                """({
                    styles, id, actionType, detail, step
                }) => {
                    // Inject styles once
                    if (!document.getElementById("agent-action-styles")) {
                        const style = document.createElement("style");
                        style.id = "agent-action-styles";
                        style.textContent = styles;
                        document.head.appendChild(style);
                    }

                    // Remove previous beam overlays
                    document.querySelectorAll(".agent-beam-wrap")
                        .forEach(el => el.remove());
                    document.querySelectorAll(".agent-highlight-target")
                        .forEach(el => el.classList
                            .remove("agent-highlight-target"));
                    document.getElementById(
                        "agent-action-bar"
                    )?.remove();

                    // Find the target element
                    let targetEl = null;
                    if (id != null) {
                        const elements = document.querySelectorAll(
                            "[data-som-id], [data-testid], "
                            + "[aria-label], button, a, "
                            + "input, select, textarea"
                        );
                        for (const el of elements) {
                            if (el.getAttribute(
                                "data-som-id"
                            ) === String(id)) {
                                targetEl = el;
                                break;
                            }
                        }
                        // Fallback: match by SoM overlay position
                        if (!targetEl) {
                            const overlays = document.querySelectorAll(
                                ".agent-som-overlay"
                            );
                            for (const overlay of overlays) {
                                if (overlay.textContent.trim()
                                    === String(id)) {
                                    const rect =
                                        overlay.getBoundingClientRect();
                                    const cx =
                                        rect.left + rect.width / 2;
                                    const cy =
                                        rect.top + rect.height / 2
                                        + 10;
                                    const hitEl =
                                        document.elementFromPoint(
                                            cx, cy
                                        );
                                    if (hitEl
                                        && hitEl !== document.body
                                        && hitEl
                                            !== document.documentElement
                                    ) {
                                        targetEl = hitEl;
                                    }
                                    break;
                                }
                            }
                        }
                    }

                    if (targetEl) {
                        const rect =
                            targetEl.getBoundingClientRect();
                        const getVisualContainer = (el) => {
                            const elRect =
                                el.getBoundingClientRect();
                            const readBr = (cs) => {
                                const tl =
                                    cs.borderTopLeftRadius;
                                if (tl && tl !== "0px")
                                    return tl;
                                const br = cs.borderRadius;
                                if (br && br !== "0px")
                                    return br;
                                return null;
                            };
                            const selfBr = readBr(
                                window.getComputedStyle(el)
                            );
                            if (selfBr) return {
                                rect: elRect,
                                borderRadius: selfBr
                            };

                            let ancestor = el.parentElement;
                            for (let i = 0;
                                i < 8 && ancestor;
                                i++
                            ) {
                                const aRect = ancestor
                                    .getBoundingClientRect();
                                if (aRect.width > elRect.width * 2.5
                                    || aRect.height
                                        > elRect.height * 2.5
                                ) break;
                                const br = readBr(
                                    window.getComputedStyle(ancestor)
                                );
                                if (br) return {
                                    rect: aRect,
                                    borderRadius: br
                                };
                                ancestor = ancestor.parentElement;
                            }
                            return {
                                rect: elRect,
                                borderRadius: "4px"
                            };
                        };
                        const container =
                            getVisualContainer(targetEl);
                        const beamRect = container.rect;
                        const br = container.borderRadius;

                        const pad = Math.max(5,
                            20 - Math.min(
                                beamRect.width, beamRect.height
                            ) / 2
                        );

                        const beam =
                            document.createElement("div");
                        beam.className =
                            "agent-beam-wrap agent-beam-active";
                        beam.id = "agent-beam-overlay";
                        beam.style.position = "fixed";
                        beam.style.left =
                            (beamRect.left - pad) + "px";
                        beam.style.top =
                            (beamRect.top - pad) + "px";
                        beam.style.width =
                            (beamRect.width + pad * 2) + "px";
                        beam.style.height =
                            (beamRect.height + pad * 2) + "px";
                        beam.style.borderRadius = br;
                        beam.style.setProperty(
                            "--beam-br", br
                        );
                        beam.style.display = "block";
                        beam.style.zIndex = "2147483646";
                        beam.style.pointerEvents = "none";
                        beam.style.overflow = "visible";

                        const bloom =
                            document.createElement("div");
                        bloom.className = "agent-beam-bloom";
                        bloom.style.borderRadius = br;
                        bloom.style.inset = "0";

                        beam.appendChild(bloom);
                        document.body.appendChild(beam);

                        targetEl.scrollIntoView({
                            behavior: "smooth",
                            block: "center"
                        });
                    } else {
                        document.body.classList.add(
                            "agent-highlight-target"
                        );
                    }

                    // Show action bar
                    const bar =
                        document.createElement("div");
                    bar.id = "agent-action-bar";
                    const parts = [];
                    if (step != null) {
                        parts.push(
                            '<span class="step">Step '
                            + step + '</span>'
                        );
                    }
                    const actionLabel = {
                        click: "Clicking",
                        type: "Typing into",
                        hover: "Hovering on",
                        scroll: "Scrolling",
                        goto: "Navigating to",
                        keypress: "Pressing key",
                        selectOption: "Selecting in",
                        upload: "Uploading to",
                        drag: "Dragging",
                        dragFile: "Dropping file on",
                    }[actionType] || actionType;
                    parts.push(
                        '<span class="action">'
                        + actionLabel + '</span>'
                    );
                    if (detail) {
                        parts.push(
                            '<span class="detail">'
                            + detail + '</span>'
                        );
                    }
                    bar.innerHTML = parts.join(" ");
                    document.body.appendChild(bar);
                }""",
                {
                    "styles": ACTION_HIGHLIGHT_STYLES,
                    "id": som_id,
                    "actionType": action_type,
                    "detail": detail,
                    "step": step,
                },
            )
        except Exception:
            logger.debug("Action highlight failed", exc_info=True)

    async def clear_action_highlight(self, delay_ms: int = 0) -> None:
        if not self.page:
            return
        if delay_ms > 0:
            await self.page.wait_for_timeout(delay_ms)
        try:
            await self.page.evaluate("""() => {
                document.querySelectorAll(
                    ".agent-beam-wrap.agent-beam-active"
                ).forEach(beam => {
                    beam.classList.remove("agent-beam-active");
                    beam.classList.add("agent-beam-fading");
                    setTimeout(() => beam.remove(), 350);
                });
                document.querySelectorAll(
                    ".agent-highlight-target"
                ).forEach(el => {
                    el.classList.remove(
                        "agent-highlight-target"
                    );
                });
                const bar = document.getElementById(
                    "agent-action-bar"
                );
                if (bar) {
                    bar.style.transition = "opacity 0.3s ease";
                    bar.style.opacity = "0";
                    setTimeout(() => bar.remove(), 300);
                }
            }""")
        except Exception:
            logger.debug("Clear highlight failed", exc_info=True)

    # ── Browser active border ──

    async def _show_browser_active_border(self) -> None:
        if not self.page or self.session_source == "attached":
            return
        try:
            await self.page.evaluate(
                """(styles) => {
                    if (document.getElementById(
                        "agent-browser-active-styles"
                    )) return;
                    const style = document.createElement("style");
                    style.id =
                        "agent-browser-active-styles";
                    style.textContent = styles;
                    document.head.appendChild(style);

                    const border = document.createElement("div");
                    border.id =
                        "agent-browser-active-border";
                    document.body.appendChild(border);

                    const bloom = document.createElement("div");
                    bloom.id =
                        "agent-browser-active-bloom";
                    document.body.appendChild(bloom);

                    const badge = document.createElement("div");
                    badge.id =
                        "agent-browser-active-badge";
                    badge.innerHTML =
                        '<span class="dot"></span>Agent Active';
                    document.body.appendChild(badge);
                }""",
                BROWSER_ACTIVE_BORDER_STYLES,
            )
        except Exception:
            logger.debug("Border injection failed", exc_info=True)

    async def _hide_browser_active_border(self) -> None:
        if not self.page:
            return
        try:
            await self.page.evaluate("""() => {
                document.getElementById(
                    "agent-browser-active-border"
                )?.remove();
                document.getElementById(
                    "agent-browser-active-bloom"
                )?.remove();
                document.getElementById(
                    "agent-browser-active-badge"
                )?.remove();
                document.getElementById(
                    "agent-browser-active-styles"
                )?.remove();
            }""")
        except Exception:
            logger.debug("Border removal failed", exc_info=True)

    async def _install_active_border_init_script(self) -> None:
        if not self.context or self.session_source == "attached":
            return
        styles = BROWSER_ACTIVE_BORDER_STYLES
        await self.context.add_init_script(
            """(css) => {
                if (!document.getElementById(
                    "agent-browser-active-styles"
                )) {
                    const style = document.createElement("style");
                    style.id =
                        "agent-browser-active-styles";
                    style.textContent = css;
                    document.head.appendChild(style);
                }
                const addBorderElements = () => {
                    if (!document.body) return;
                    if (!document.getElementById(
                        "agent-browser-active-border"
                    )) {
                        const border =
                            document.createElement("div");
                        border.id =
                            "agent-browser-active-border";
                        document.body.appendChild(border);
                    }
                    if (!document.getElementById(
                        "agent-browser-active-bloom"
                    )) {
                        const bloom =
                            document.createElement("div");
                        bloom.id =
                            "agent-browser-active-bloom";
                        document.body.appendChild(bloom);
                    }
                    if (!document.getElementById(
                        "agent-browser-active-badge"
                    )) {
                        const badge =
                            document.createElement("div");
                        badge.id =
                            "agent-browser-active-badge";
                        badge.innerHTML =
                            '<span class="dot"></span>'
                            + 'Agent Active';
                        document.body.appendChild(badge);
                    }
                };
                if (document.body) {
                    addBorderElements();
                } else {
                    document.addEventListener(
                        "DOMContentLoaded", addBorderElements
                    );
                }
            }""",
            styles,
        )

    # ── Target resolution ──

    async def _resolve_target(self, som_id: int) -> dict[str, Any]:
        await self.ensure_started()
        target = await refresh_target(
            self.page, self.interactable_elements, som_id
        )
        self.interactable_elements = [
            {**el, **({"id": target} if el.get("id") == som_id else {})}
            for el in self.interactable_elements
        ]
        return target

    # ── Page actions ──

    async def goto(self, url: str) -> dict[str, Any]:
        await self.ensure_started()
        short_url = url[:60] + "…" if len(url) > 60 else url
        await self.show_action_highlight(None, "goto", short_url)
        await self.page.goto(
            url, wait_until="domcontentloaded", timeout=15000
        )
        await self._smart_wait()

        self.interactable_elements = await collect_interactable_elements(
            self.page, draw_overlays=True
        )
        screenshot_path = self.artifacts.get_current_view_path()
        await self.page.screenshot(
            path=str(screenshot_path), full_page=True
        )
        await clear_overlays(self.page)

        accessibility_tree = None
        try:
            accessibility_tree = await self.page.locator(
                ":root"
            ).aria_snapshot(timeout=5000)
        except Exception:
            logger.debug("ariaSnapshot unavailable")

        page_content = await collect_page_content(self.page)
        metadata = await self.get_page_metadata(500)

        self.artifacts.append_event("goto", {
            "url": url,
            "elementCount": len(self.interactable_elements),
            "hasA11yTree": accessibility_tree is not None,
            "screenshotPath": str(screenshot_path),
        })
        await self.clear_action_highlight()
        await self._show_browser_active_border()

        captcha_challenge = await detect_captcha_challenge(self.page)
        if captcha_challenge:
            self.artifacts.append_event("captcha_detected", {
                "url": url,
                "types": [c["type"] for c in captcha_challenge],
            })

        return {
            "url": url,
            "metadata": metadata,
            "elements": self.interactable_elements,
            "accessibilityTree": accessibility_tree,
            "pageContent": page_content,
            "screenshotPath": str(screenshot_path),
            "captchaChallenge": captcha_challenge,
        }

    async def observe(self) -> dict[str, Any]:
        await self.ensure_started()
        self.interactable_elements = (
            await collect_interactable_elements(
                self.page, draw_overlays=True
            )
        )

        screenshot_path = self.artifacts.get_current_view_path()
        await self.page.screenshot(
            path=str(screenshot_path), full_page=True
        )
        await clear_overlays(self.page)

        accessibility_tree = None
        try:
            accessibility_tree = await self.page.locator(
                ":root"
            ).aria_snapshot(timeout=5000)
        except Exception:
            logger.debug("ariaSnapshot unavailable")

        page_content = await collect_page_content(self.page)
        recent_errors = [
            e for e in self.page_errors if e.get("message")
        ][-5:]
        tabs = await self.get_tabs_list()

        captcha_challenge = await detect_captcha_challenge(self.page)
        if captcha_challenge:
            self.artifacts.append_event("captcha_detected", {
                "types": [c["type"] for c in captcha_challenge],
            })

        self.artifacts.append_event("observe", {
            "count": len(self.interactable_elements),
            "hasA11yTree": accessibility_tree is not None,
            "screenshotPath": str(screenshot_path),
            "tabCount": len(tabs),
            "captchaDetected": captcha_challenge is not None,
        })

        result: dict[str, Any] = {
            "elements": self.interactable_elements,
            "accessibilityTree": accessibility_tree,
            "pageContent": page_content,
            "screenshotPath": str(screenshot_path),
            "tabs": tabs,
            "captchaChallenge": captcha_challenge,
        }
        if recent_errors:
            result["recentErrors"] = recent_errors
        return result

    async def click(self, som_id: int) -> dict[str, Any]:
        target = await self._resolve_target(som_id)
        label = target.get("text") or target.get("tag") or f"#{som_id}"
        await self.show_action_highlight(som_id, "click", label)
        await self.page.mouse.click(target["x"], target["y"])
        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"click-{som_id}"
        )
        self.artifacts.append_event("click", {
            "id": som_id,
            "target": target,
            "screenshotPath": screenshot_path,
        })
        return {
            "id": som_id,
            "target": target,
            "screenshotPath": screenshot_path,
        }

    async def type_text(
        self,
        som_id: int,
        text: str,
        *,
        submit: bool = False,
    ) -> dict[str, Any]:
        target = await self._resolve_target(som_id)
        short_text = text[:30] + "…" if len(text) > 30 else text
        detail = f'"{short_text}"{" + Enter" if submit else ""}'
        await self.show_action_highlight(som_id, "type", detail)
        locator = self.page.locator(f'[data-som-id="{som_id}"]')
        try:
            await locator.click(timeout=2000)
            await locator.fill("")
        except Exception:
            logger.debug("Locator fill fallback", exc_info=True)
            await self.page.mouse.click(
                target["x"], target["y"], click_count=3
            )
        await self.page.wait_for_timeout(100)
        await self.page.keyboard.type(text)
        if submit:
            await self.page.keyboard.press("Enter")
        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"type-{som_id}"
        )
        self.artifacts.append_event("type", {
            "id": som_id,
            "text": text,
            "submit": submit,
            "target": target,
            "screenshotPath": screenshot_path,
        })
        return {
            "id": som_id,
            "target": target,
            "text": text,
            "submit": submit,
            "screenshotPath": screenshot_path,
        }

    async def hover(self, som_id: int) -> dict[str, Any]:
        target = await self._resolve_target(som_id)
        label = target.get("text") or target.get("tag") or f"#{som_id}"
        await self.show_action_highlight(som_id, "hover", label)
        await self.page.mouse.move(target["x"], target["y"])
        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"hover-{som_id}"
        )
        self.artifacts.append_event("hover", {
            "id": som_id,
            "target": target,
            "screenshotPath": screenshot_path,
        })
        return {
            "id": som_id,
            "target": target,
            "screenshotPath": screenshot_path,
        }

    async def evaluate(self, expression: str) -> dict[str, Any]:
        await self.ensure_started()
        is_error = False
        try:
            result = await self.page.evaluate(
                f"(() => {{ {expression} }})()"
            )
        except Exception as exc:
            result = str(exc)
            is_error = True

        if isinstance(result, (dict, list)):
            serialized = json.dumps(result, indent=2, default=str)[
                :8192
            ]
        else:
            serialized = str(result if result is not None else "")[
                :8192
            ]
        self.artifacts.append_event("evaluate", {
            "isError": is_error,
            "length": len(serialized),
        })
        return {"result": serialized, "isError": is_error}

    async def select_option(
        self, som_id: int, values: list[str] | str
    ) -> dict[str, Any]:
        target = await self._resolve_target(som_id)
        value_array = values if isinstance(values, list) else [values]
        await self.show_action_highlight(
            som_id, "selectOption", ", ".join(value_array)
        )

        select_handle = await self.page.evaluate_handle(
            """(targetInfo) => {
                const elements =
                    document.querySelectorAll("select");
                for (const el of elements) {
                    const rect = el.getBoundingClientRect();
                    const cx = rect.x + rect.width / 2;
                    const cy = rect.y + rect.height / 2;
                    if (Math.abs(cx - targetInfo.x) < 10
                        && Math.abs(cy - targetInfo.y) < 10
                    ) {
                        return el;
                    }
                }
                for (const el of elements) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0)
                        return el;
                }
                return null;
            }""",
            {"x": target["x"], "y": target["y"]},
        )

        if not select_handle:
            raise RuntimeError(
                f"No <select> element found near element {som_id}."
            )

        await select_handle.select_option(value_array)
        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"select-{som_id}"
        )
        self.artifacts.append_event("selectOption", {
            "id": som_id,
            "values": value_array,
            "target": target,
            "screenshotPath": screenshot_path,
        })
        return {
            "id": som_id,
            "values": value_array,
            "target": target,
            "screenshotPath": screenshot_path,
        }

    async def handle_dialog(
        self,
        *,
        action: str = "accept",
        prompt_text: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()

        dialog_info: dict[str, Any] | None = None
        dialog_event = asyncio.Event()

        async def _dialog_handler(dialog: Any) -> None:
            nonlocal dialog_info
            self.page.remove_listener("dialog", _dialog_handler)
            dialog_info = {
                "type": dialog.type,
                "message": dialog.message,
                "defaultValue": dialog.default_value,
            }
            if action == "dismiss":
                await dialog.dismiss()
            else:
                await dialog.accept(prompt_text or None)
            dialog_event.set()

        self.page.on("dialog", _dialog_handler)

        try:
            await asyncio.wait_for(dialog_event.wait(), timeout=3.0)
        except TimeoutError:
            self.page.remove_listener("dialog", _dialog_handler)

        if not dialog_info:
            return {
                "handled": False,
                "message": "No dialog appeared within 3 seconds.",
            }

        screenshot_path = await self._capture_step_screenshot(
            f"dialog-{action}"
        )
        self.artifacts.append_event("handleDialog", {
            "action": action,
            "dialogInfo": dialog_info,
            "screenshotPath": screenshot_path,
        })
        return {
            "handled": True,
            "action": action,
            "dialogInfo": dialog_info,
            "screenshotPath": screenshot_path,
        }

    async def go_back(self) -> dict[str, Any]:
        await self.ensure_started()
        await self.page.go_back(
            wait_until="domcontentloaded", timeout=15000
        )
        await self._smart_wait()
        screenshot_path = await self._capture_step_screenshot("goback")
        self.artifacts.append_event("goBack", {
            "url": self.page.url,
            "screenshotPath": screenshot_path,
        })
        return {
            "url": self.page.url,
            "screenshotPath": screenshot_path,
        }

    async def keypress(self, key: str) -> dict[str, Any]:
        await self.ensure_started()
        await self.show_action_highlight(None, "keypress", key)
        await self.page.keyboard.press(key)
        await self._smart_wait()
        await self.clear_action_highlight(300)
        screenshot_path = await self._capture_step_screenshot(
            f"keypress-{key}"
        )
        self.artifacts.append_event("keypress", {
            "key": key,
            "screenshotPath": screenshot_path,
        })
        return {"key": key, "screenshotPath": screenshot_path}

    async def scroll(self, direction: str) -> dict[str, Any]:
        await self.ensure_started()
        await self.show_action_highlight(None, "scroll", direction)
        await self.page.evaluate(
            """(dir) => {
                const h = window.innerHeight;
                if (dir === "down")
                    window.scrollBy({
                        top: h * 0.8, behavior: "smooth"
                    });
                else if (dir === "up")
                    window.scrollBy({
                        top: -h * 0.8, behavior: "smooth"
                    });
                else if (dir === "top")
                    window.scrollTo({
                        top: 0, behavior: "smooth"
                    });
                else if (dir === "bottom")
                    window.scrollTo({
                        top: document.body.scrollHeight,
                        behavior: "smooth"
                    });
            }""",
            direction,
        )
        await self._smart_wait()
        await self.clear_action_highlight(300)
        screenshot_path = await self._capture_step_screenshot(
            f"scroll-{direction}"
        )
        self.artifacts.append_event("scroll", {
            "direction": direction,
            "screenshotPath": screenshot_path,
        })
        return {"direction": direction, "screenshotPath": screenshot_path}

    async def wait_for(
        self,
        *,
        text: str | None = None,
        text_gone: str | None = None,
        selector: str | None = None,
        timeout: int = 30000,
    ) -> dict[str, Any]:
        await self.ensure_started()

        conditions: list[str] = []
        if text:
            conditions.append("text")
        if text_gone:
            conditions.append("textGone")
        if selector:
            conditions.append("selector")
        if not conditions:
            raise ValueError(
                "waitFor requires at least one of: text, textGone, selector."
            )

        started_at = time.monotonic()
        timeout_ms = max(1000, min(timeout, 300000))
        result: dict[str, Any] = {
            "matched": None,
            "waitedMs": 0,
            "condition": conditions[0],
        }

        try:
            if text:
                await self.page.wait_for_function(
                    """(expected) => {
                        const bodyText =
                            document.body?.innerText || "";
                        if (bodyText.includes(expected))
                            return true;
                        const ph = "place" + "holder";
                        const checkAttrs = [
                            ph, "aria-label", "title",
                            "data-testid"
                        ];
                        for (const attr of checkAttrs) {
                            for (const el of document
                                .querySelectorAll(
                                    '[' + attr + ']'
                                )
                            ) {
                                const val =
                                    el.getAttribute(attr) || "";
                                if (val.includes(expected))
                                    return true;
                            }
                        }
                        for (const el of document.querySelectorAll(
                            "input, textarea"
                        )) {
                            const val = typeof el.value
                                === "string" ? el.value : "";
                            if (val.includes(expected))
                                return true;
                        }
                        return false;
                    }""",
                    text,
                    timeout=timeout_ms,
                    polling=500,
                )
                result["matched"] = "text"

            if text_gone:
                await self.page.wait_for_function(
                    """(expected) => {
                        const bodyText =
                            document.body?.innerText || "";
                        if (bodyText.includes(expected))
                            return false;
                        const ph = "place" + "holder";
                        const checkAttrs = [
                            ph, "aria-label", "title",
                            "data-testid"
                        ];
                        for (const attr of checkAttrs) {
                            for (const el of document
                                .querySelectorAll(
                                    '[' + attr + ']'
                                )
                            ) {
                                const val =
                                    el.getAttribute(attr) || "";
                                if (val.includes(expected))
                                    return false;
                            }
                        }
                        for (const el of document.querySelectorAll(
                            "input, textarea"
                        )) {
                            const val = typeof el.value
                                === "string" ? el.value : "";
                            if (val.includes(expected))
                                return false;
                        }
                        return true;
                    }""",
                    text_gone,
                    timeout=timeout_ms,
                    polling=500,
                )
                result["matched"] = "textGone"

            if selector:
                await self.page.wait_for_selector(
                    selector,
                    timeout=timeout_ms,
                    state="visible",
                )
                result["matched"] = "selector"
        except Exception as exc:
            result["matched"] = None
            result["timedOut"] = True
            result["error"] = str(exc)

        result["waitedMs"] = int(
            (time.monotonic() - started_at) * 1000
        )
        matched_label = result["matched"] or "timeout"
        screenshot_path = await self._capture_step_screenshot(
            f"waitfor-{matched_label}"
        )
        self.artifacts.append_event("waitFor", {
            **result,
            "screenshotPath": screenshot_path,
        })
        return {**result, "screenshotPath": screenshot_path}

    async def upload(
        self,
        som_id: int,
        *,
        paths: list[str] | None = None,
        files: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        paths = paths or []
        files = files or []
        target = await self._resolve_target(som_id)
        await self.show_action_highlight(
            som_id, "upload", f"{len(paths) + len(files)} file(s)"
        )
        base64_paths = write_base64_files(files) if files else []
        all_paths = [*paths, *base64_paths]

        if not all_paths:
            raise ValueError(
                "No files provided. Supply 'paths' (local file paths) "
                "or 'files' (base64-encoded file objects)."
            )

        for fp in all_paths:
            if not Path(fp).exists():
                raise FileNotFoundError(f"File not found: {fp}")

        file_input = await self.page.evaluate_handle(
            """(targetInfo) => {
                const elements = document.querySelectorAll(
                    'input[type="file"]'
                );
                if (targetInfo.tag === "input"
                    && targetInfo.type === "file"
                ) {
                    for (const el of elements) {
                        const rect = el.getBoundingClientRect();
                        const cx = rect.x + rect.width / 2;
                        const cy = rect.y + rect.height / 2;
                        if (Math.abs(cx - targetInfo.x) < 5
                            && Math.abs(cy - targetInfo.y) < 5
                        ) {
                            return el;
                        }
                    }
                }
                for (const el of elements) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0)
                        return el;
                }
                return null;
            }""",
            {
                "tag": target.get("tag"),
                "type": target.get("type"),
                "x": target["x"],
                "y": target["y"],
            },
        )

        is_visible = False
        try:
            is_visible = await file_input.is_visible()
        except Exception:
            pass

        if not file_input or not is_visible:
            async with self.page.expect_file_chooser(
                timeout=5000
            ) as fc_info:
                await self.page.mouse.click(target["x"], target["y"])
            chooser = await fc_info.value
            await chooser.set_files(all_paths)
        else:
            await file_input.set_input_files(all_paths)

        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"upload-{som_id}"
        )
        self.artifacts.append_event("upload", {
            "id": som_id,
            "target": target,
            "fileCount": len(all_paths),
            "screenshotPath": screenshot_path,
        })
        return {
            "id": som_id,
            "target": target,
            "fileCount": len(all_paths),
            "screenshotPath": screenshot_path,
        }

    async def drag(self, from_id: int, to_id: int) -> dict[str, Any]:
        from_target = await self._resolve_target(from_id)
        to_target = await self._resolve_target(to_id)
        await self.show_action_highlight(
            from_id, "drag", f"→ #{to_id}"
        )

        await self.page.mouse.move(from_target["x"], from_target["y"])
        await self.page.wait_for_timeout(100)
        await self.page.mouse.down()

        steps = 6
        for i in range(1, steps + 1):
            progress = i / steps
            x = from_target["x"] + (
                to_target["x"] - from_target["x"]
            ) * progress
            y = from_target["y"] + (
                to_target["y"] - from_target["y"]
            ) * progress
            await self.page.mouse.move(x, y)
            await self.page.wait_for_timeout(30)

        await self.page.mouse.move(to_target["x"], to_target["y"])
        await self.page.mouse.up()

        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"drag-{from_id}-to-{to_id}"
        )
        self.artifacts.append_event("drag", {
            "fromId": from_id,
            "toId": to_id,
            "from": from_target,
            "to": to_target,
            "screenshotPath": screenshot_path,
        })
        return {
            "fromId": from_id,
            "toId": to_id,
            "from": from_target,
            "to": to_target,
            "screenshotPath": screenshot_path,
        }

    async def drag_file(
        self,
        *,
        paths: list[str] | None = None,
        files: list[dict[str, str]] | None = None,
        to_id: int | None = None,
    ) -> dict[str, Any]:
        paths = paths or []
        files = files or []
        base64_paths = write_base64_files(files) if files else []
        all_paths = [*paths, *base64_paths]

        if not all_paths:
            raise ValueError(
                "No files provided. Supply 'paths' (local file paths) "
                "or 'files' (base64-encoded file objects)."
            )

        for fp in all_paths:
            if not Path(fp).exists():
                raise FileNotFoundError(f"File not found: {fp}")

        target = await self._resolve_target(to_id)
        await self.show_action_highlight(
            to_id, "dragFile", f"{len(all_paths)} file(s)"
        )

        drop_accepted = await self.page.evaluate(
            """async ({ x, y, filePaths }) => {
                const elementAtPoint =
                    document.elementFromPoint(x, y);
                if (!elementAtPoint) return false;

                const dropZone = elementAtPoint.closest(
                    "[class*='drop'], "
                    + "[class*='upload'], "
                    + "[data-testid*='drop'], "
                    + "[role='presentation']"
                ) || elementAtPoint;

                const fileInput = dropZone.querySelector(
                    'input[type="file"]'
                ) || dropZone.closest("form")
                    ?.querySelector('input[type="file"]');

                if (fileInput) {
                    return { hasFileInput: true };
                }

                const dataTransfer = new DataTransfer();
                for (const eventType of [
                    "dragenter", "dragover", "drop"
                ]) {
                    const event = new DragEvent(eventType, {
                        bubbles: true,
                        cancelable: true,
                        dataTransfer,
                        clientX: x,
                        clientY: y,
                    });
                    elementAtPoint.dispatchEvent(event);
                }

                return { hasFileInput: false };
            }""",
            {"x": target["x"], "y": target["y"], "filePaths": all_paths},
        )

        if drop_accepted and drop_accepted.get("hasFileInput"):
            file_input = await self.page.evaluate_handle(
                """({ x, y }) => {
                    const elementAtPoint =
                        document.elementFromPoint(x, y);
                    if (!elementAtPoint) return null;
                    const dropZone = elementAtPoint.closest(
                        "[class*='drop'], "
                        + "[class*='upload'], "
                        + "[data-testid*='drop'], "
                        + "[role='presentation']"
                    ) || elementAtPoint;
                    return dropZone.querySelector(
                        'input[type="file"]'
                    ) || dropZone.closest("form")
                        ?.querySelector('input[type="file"]');
                }""",
                {"x": target["x"], "y": target["y"]},
            )
            if file_input:
                await file_input.set_input_files(all_paths)

        await self._smart_wait()
        await self.clear_action_highlight(400)
        screenshot_path = await self._capture_step_screenshot(
            f"dragfile-to-{to_id}"
        )
        self.artifacts.append_event("dragFile", {
            "toId": to_id,
            "target": target,
            "fileCount": len(all_paths),
            "screenshotPath": screenshot_path,
        })
        return {
            "toId": to_id,
            "target": target,
            "fileCount": len(all_paths),
            "screenshotPath": screenshot_path,
        }

    # ── Tab management ──

    async def get_tabs_list(self) -> list[dict[str, Any]]:
        if not self.context:
            return []
        pages = self.context.pages
        current_page_index = (
            pages.index(self.page) if self.page in pages else -1
        )
        tabs: list[dict[str, Any]] = []
        for i, p in enumerate(pages):
            title = ""
            try:
                title = await p.title()
            except Exception:
                pass
            tabs.append({
                "index": i,
                "url": p.url,
                "title": title,
                "active": i == current_page_index,
            })
        return tabs

    async def tab_action(
        self,
        action: str,
        *,
        index: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()

        if action == "list":
            return {"tabs": await self.get_tabs_list()}

        if action == "new":
            new_page = await self.context.new_page()
            if url:
                try:
                    await new_page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                except Exception:
                    pass
            self.page = new_page
            self._attach_page_observers()
            self.interactable_elements = []
            screenshot_path = await self._capture_step_screenshot(
                "tab-new"
            )
            return {
                "url": self.page.url,
                "index": len(self.context.pages) - 1,
                "screenshotPath": screenshot_path,
            }

        if action == "close":
            pages = self.context.pages
            target_index = (
                index if index is not None else pages.index(self.page)
            )
            target_page = (
                pages[target_index]
                if 0 <= target_index < len(pages)
                else None
            )
            if not target_page:
                raise IndexError(
                    f"Tab index {target_index} not found."
                )
            await target_page.close()

            if target_page is self.page:
                remaining = self.context.pages
                if remaining:
                    self.page = remaining[-1]
                    self._attach_page_observers()
                else:
                    self.page = None
            self.interactable_elements = []
            return {
                "closed": target_index,
                "remaining": len(self.context.pages),
            }

        if action == "select":
            pages = self.context.pages
            target_index = (
                index if index is not None else len(pages) - 1
            )
            target_page = (
                pages[target_index]
                if 0 <= target_index < len(pages)
                else None
            )
            if not target_page:
                raise IndexError(
                    f"Tab index {target_index} not found."
                )
            if target_page is self.page:
                return {
                    "active": target_index,
                    "url": self.page.url,
                }
            self.page = target_page
            self._attach_page_observers()
            self.interactable_elements = []
            screenshot_path = await self._capture_step_screenshot(
                f"tab-select-{target_index}"
            )
            return {
                "active": target_index,
                "url": self.page.url,
                "screenshotPath": screenshot_path,
            }

        raise ValueError(
            f"Unknown tab action: {action}. "
            "Use: list, new, close, select."
        )

    # ── Metadata ──

    async def get_page_metadata(
        self, text_limit: int = 1500
    ) -> dict[str, Any]:
        await self.ensure_started()
        title = ""
        try:
            title = await self.page.title()
        except Exception:
            pass
        text_preview = ""
        try:
            text_preview = await self.page.evaluate(
                """(limit) => {
                    const bodyText =
                        document.body?.innerText || "";
                    return bodyText.replace(/\\s+/g, " ")
                        .trim().slice(0, limit);
                }""",
                text_limit,
            )
        except Exception:
            pass
        return {
            "url": self.page.url,
            "title": title,
            "textPreview": text_preview or "",
        }

    async def screenshot_base64(self) -> str:
        await self.ensure_started()
        import base64

        buf = await self.page.screenshot(type="png")
        return base64.b64encode(buf).decode("ascii")

    # ── CDP health / diagnostics ──

    async def get_cdp_health(
        self, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import aiohttp

        options = options or {}
        timeout_ms = (
            int(options.get("timeoutMs", 3000))
            if options.get("timeoutMs") is not None
            else 3000
        )
        timeout_ms = max(1000, timeout_ms)
        endpoint = str(
            options.get("endpoint") or self.cdp_endpoint or ""
        ).strip()
        if not endpoint:
            return {
                "ok": False,
                "endpoint": None,
                "versionUrl": None,
                "error": "CDP endpoint is empty.",
            }

        version_url = endpoint
        if "/json/version" not in version_url:
            try:
                from urllib.parse import urljoin

                version_url = urljoin(
                    endpoint, "/json/version"
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "endpoint": endpoint,
                    "versionUrl": None,
                    "error": f"Invalid CDP endpoint: {exc}",
                }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    version_url,
                    timeout=aiohttp.ClientTimeout(
                        total=timeout_ms / 1000
                    ),
                    headers={"Accept": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        return {
                            "ok": False,
                            "endpoint": endpoint,
                            "versionUrl": version_url,
                            "status": resp.status,
                            "error": (
                                f"Unexpected status {resp.status}."
                            ),
                        }
                    data = await resp.json()
                    return {
                        "ok": True,
                        "endpoint": endpoint,
                        "versionUrl": version_url,
                        "browser": data.get("Browser"),
                        "protocolVersion": data.get(
                            "Protocol-Version"
                        ),
                        "webSocketDebuggerUrl": data.get(
                            "webSocketDebuggerUrl"
                        ),
                        "userAgent": data.get("User-Agent"),
                    }
        except TimeoutError:
            return {
                "ok": False,
                "endpoint": endpoint,
                "versionUrl": version_url,
                "error": f"Timed out after {timeout_ms}ms.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "versionUrl": version_url,
                "error": str(exc),
            }

    async def get_cdp_diagnostics(
        self, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import aiohttp

        options = options or {}
        timeout_ms = (
            int(options.get("timeoutMs", 3000))
            if options.get("timeoutMs") is not None
            else 3000
        )
        endpoint = str(
            options.get("endpoint") or self.cdp_endpoint or ""
        ).strip()
        health = await self.get_cdp_health({
            "endpoint": endpoint,
            "timeoutMs": timeout_ms,
        })
        hints: list[str] = []
        warnings: list[str] = []

        targets: dict[str, Any] = {
            "endpoint": endpoint or None,
            "listUrl": None,
            "count": None,
            "pageTargets": None,
            "serviceWorkerTargets": None,
            "browserTargets": None,
            "sample": [],
            "error": None,
        }

        if not health.get("ok"):
            health_error = str(health.get("error", ""))
            if health.get("status") == 404:
                hints.append(
                    "CDP endpoint is reachable but /json/version "
                    "returned 404. Enable remote debugging from "
                    "chrome://inspect/#remote-debugging and verify "
                    "the endpoint."
                )
            elif "Timed out" in health_error:
                hints.append(
                    "CDP endpoint timed out. Ensure Chrome is "
                    "running and remote debugging is enabled."
                )
            elif (
                "fetch failed" in health_error.lower()
                or "econnrefused" in health_error.lower()
            ):
                hints.append(
                    "CDP endpoint refused the connection. Start "
                    "Chrome remote debugging and check the "
                    "host/port."
                )
            elif "invalid cdp endpoint" in health_error.lower():
                hints.append(
                    "CDP endpoint format is invalid. Use a URL "
                    "like http://127.0.0.1:9222."
                )
            else:
                hints.append(
                    "CDP health check failed. Confirm "
                    "chrome://inspect/#remote-debugging is "
                    "enabled for the browser instance."
                )

            hints.append(
                "If you continue with browser_source=auto, the "
                "runtime will fall back to managed mode."
            )
            return {
                "ok": False,
                "endpoint": endpoint or None,
                "health": health,
                "targets": targets,
                "hints": hints,
                "warnings": warnings,
            }

        try:
            from urllib.parse import urljoin

            health_version_url = health.get("versionUrl", "")
            list_url = (
                urljoin(health_version_url, "/json/list")
                if health_version_url
                else urljoin(endpoint, "/json/list")
            )
            targets["listUrl"] = list_url

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    list_url,
                    headers={"Accept": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        targets["error"] = (
                            f"Unexpected status {resp.status} "
                            "from /json/list."
                        )
                        warnings.append(targets["error"])
                    else:
                        raw_list = await resp.json()
                        typed = (
                            raw_list
                            if isinstance(raw_list, list)
                            else []
                        )
                        by_type: dict[str, int] = {}
                        for item in typed:
                            t = item.get("type", "unknown")
                            by_type[t] = by_type.get(t, 0) + 1
                        targets["count"] = len(typed)
                        targets["pageTargets"] = by_type.get(
                            "page", 0
                        )
                        targets[
                            "serviceWorkerTargets"
                        ] = by_type.get("service_worker", 0)
                        targets[
                            "browserTargets"
                        ] = by_type.get("browser", 0)
                        targets["sample"] = typed[:3]
                        if targets["pageTargets"] == 0:
                            warnings.append(
                                "No page targets are open in the "
                                "attached profile. Open a regular "
                                "Chrome tab before starting an "
                                "attached run."
                            )
        except Exception as exc:
            targets["error"] = str(exc)
            warnings.append(
                f"Could not inspect /json/list: {exc}"
            )

        if not health.get("webSocketDebuggerUrl"):
            warnings.append(
                "No webSocketDebuggerUrl was reported by "
                "/json/version. Attach may be unstable."
            )

        if not warnings:
            hints.append(
                "CDP endpoint looks healthy. Attached mode "
                "should be available."
            )
        else:
            hints.append(
                "CDP endpoint is reachable, but diagnostics "
                "found issues that may affect attached runs."
            )
            hints.append(
                "You can still run with browser_source=auto to "
                "attach when possible and fall back to managed "
                "mode."
            )

        return {
            "ok": len(warnings) == 0,
            "endpoint": endpoint or None,
            "health": health,
            "targets": targets,
            "hints": hints,
            "warnings": warnings,
        }

    def get_chrome_launcher_info(self) -> dict[str, Any]:
        return self.chrome_launcher.get_debug_info()

    # ── Text layout audit ──

    async def audit_text_layout(
        self, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        options = options or {}
        limit = (
            max(1, min(500, int(options["limit"])))
            if options.get("limit") is not None
            else 80
        )
        selectors = (
            options["selectors"].strip()
            if isinstance(options.get("selectors"), str)
            and options["selectors"].strip()
            else DEFAULT_TEXT_AUDIT_SELECTORS
        )
        overflow_threshold = (
            max(0, float(options["overflowThreshold"]))
            if options.get("overflowThreshold") is not None
            else 1
        )

        await self.ensure_started()

        audit = await self.page.evaluate(
            """({ limit, selectors, overflowThreshold }) => {
                function parsePx(value, fallback = 0) {
                    if (typeof value !== "string")
                        return fallback;
                    const num = Number.parseFloat(value);
                    return Number.isFinite(num) ? num : fallback;
                }

                function toSelector(node) {
                    if (!node || node.nodeType !== 1) return "";
                    if (node.id) {
                        const escaped =
                            typeof CSS !== "undefined"
                            && typeof CSS.escape === "function"
                            ? CSS.escape(node.id)
                            : node.id.replace(
                                /[^a-zA-Z0-9_-]/g, ""
                            );
                        return '#' + escaped;
                    }
                    const parts = [];
                    let current = node;
                    while (current
                        && current.nodeType === 1
                        && parts.length < 4
                    ) {
                        const parent = current.parentElement;
                        const tag =
                            current.tagName.toLowerCase();
                        if (!parent) {
                            parts.unshift(tag);
                            break;
                        }
                        const siblings = Array.from(
                            parent.children
                        ).filter(
                            c => c.tagName === current.tagName
                        );
                        if (siblings.length === 1) {
                            parts.unshift(tag);
                        } else {
                            const idx =
                                siblings.indexOf(current) + 1;
                            parts.unshift(
                                tag + ':nth-of-type(' + idx + ')'
                            );
                        }
                        current = parent;
                    }
                    return parts.join(" > ");
                }

                function toGraphemes(text) {
                    if (!text) return [];
                    if (typeof Intl !== "undefined"
                        && typeof Intl.Segmenter === "function"
                    ) {
                        const segmenter = new Intl.Segmenter(
                            undefined,
                            { granularity: "grapheme" }
                        );
                        return Array.from(
                            segmenter.segment(text),
                            e => e.segment
                        );
                    }
                    return Array.from(text);
                }

                function estimateLineCount(
                    text, maxWidth, font
                ) {
                    if (!text) return 0;
                    if (!Number.isFinite(maxWidth)
                        || maxWidth <= 0
                    ) return 1;
                    const canvas =
                        document.createElement("canvas");
                    const context =
                        canvas.getContext("2d");
                    if (!context) return null;
                    context.font = font;
                    const paragraphs = text.split("\\n");
                    let lineCount = 0;
                    for (const paragraph of paragraphs) {
                        if (!paragraph.length) {
                            lineCount += 1;
                            continue;
                        }
                        let lineWidth = 0;
                        let activeLine = 1;
                        const graphemes =
                            toGraphemes(paragraph);
                        for (const g of graphemes) {
                            const width =
                                context.measureText(g).width;
                            if (lineWidth + width > maxWidth
                                && lineWidth > 0
                            ) {
                                activeLine += 1;
                                lineWidth = width;
                            } else {
                                lineWidth += width;
                            }
                        }
                        lineCount += activeLine;
                    }
                    return lineCount;
                }

                const result = [];
                const elements = Array.from(
                    document.querySelectorAll(selectors)
                );
                for (const node of elements) {
                    if (result.length >= limit) break;
                    if (!(node instanceof HTMLElement)) continue;
                    const style =
                        window.getComputedStyle(node);
                    if (style.display === "none"
                        || style.visibility === "hidden"
                        || style.opacity === "0"
                    ) continue;

                    const text = (
                        node.innerText
                        || node.textContent || ""
                    ).trim();
                    if (!text) continue;

                    const lineHeight = parsePx(
                        style.lineHeight,
                        parsePx(style.fontSize, 16) * 1.2
                    );
                    const font =
                        style.font
                        && style.font !== (
                            "normal normal normal normal "
                            + "16px / normal sans-serif"
                        )
                        ? style.font
                        : style.fontWeight + " "
                          + style.fontSize + " "
                          + style.fontFamily;
                    const paddingLeft = parsePx(
                        style.paddingLeft, 0
                    );
                    const paddingRight = parsePx(
                        style.paddingRight, 0
                    );
                    const maxTextWidth = Math.max(
                        0,
                        node.clientWidth
                        - paddingLeft - paddingRight
                    );
                    const normalizedText =
                        style.whiteSpace.startsWith("pre")
                        ? text
                        : text.replace(/\\s+/g, " ");
                    const estimatedLineCount =
                        estimateLineCount(
                            normalizedText, maxTextWidth, font
                        );
                    const expectedHeight =
                        Number.isFinite(estimatedLineCount)
                        ? estimatedLineCount * lineHeight
                        : null;

                    const hasActualOverflow =
                        (node.scrollWidth - node.clientWidth
                            > overflowThreshold)
                        || (node.scrollHeight
                            - node.clientHeight
                            > overflowThreshold);
                    const hasEstimatedOverflow =
                        Number.isFinite(expectedHeight)
                        ? expectedHeight - node.clientHeight
                            > overflowThreshold
                        : false;

                    if (!hasActualOverflow
                        && !hasEstimatedOverflow
                    ) continue;

                    result.push({
                        selector: toSelector(node),
                        tag: node.tagName.toLowerCase(),
                        text: normalizedText.slice(0, 200),
                        whiteSpace: style.whiteSpace,
                        font: font,
                        lineHeight: lineHeight,
                        maxTextWidth: maxTextWidth,
                        clientHeight: node.clientHeight,
                        scrollHeight: node.scrollHeight,
                        clientWidth: node.clientWidth,
                        scrollWidth: node.scrollWidth,
                        estimatedLineCount: estimatedLineCount,
                        expectedHeight: expectedHeight,
                        hasActualOverflow: hasActualOverflow,
                        hasEstimatedOverflow: hasEstimatedOverflow,
                    });
                }

                return {
                    summary: {
                        checkedElements: elements.length,
                        flaggedElements: result.length,
                        hasIssues: result.length > 0,
                    },
                    flaggedElements: result,
                };
            }""",
            {
                "limit": limit,
                "selectors": selectors,
                "overflowThreshold": overflow_threshold,
            },
        )

        self.artifacts.append_event("text_layout_audit", {
            **audit.get("summary", {}),
            "limit": limit,
            "selectors": selectors,
        })

        return {
            **audit,
            "page": await self.get_page_metadata(400),
            "options": {
                "limit": limit,
                "selectors": selectors,
                "overflowThreshold": overflow_threshold,
            },
        }

    # ── Diagnostics / artifacts ──

    def get_debug_state(
        self, limit: int = 20
    ) -> dict[str, Any]:
        artifacts = (
            self.artifacts.get_summary()
            if self.artifacts.session_dir
            else self.last_session_summary
        )
        capabilities = (
            {
                "sessionReuse": True,
                "visibleBrowser": True,
                "videos": self.attached_video_capability,
                "traces": self.trace_active,
                "modeSwitching": False,
            }
            if self.session_source == "attached"
            else {
                "sessionReuse": True,
                "visibleBrowser": self.browser_mode == "headful",
                "videos": True,
                "traces": True,
                "modeSwitching": True,
            }
        )

        return {
            "active": self.browser is not None,
            "requestedSource": self.requested_source,
            "sessionSource": self.session_source,
            "autoFallbackReason": self.auto_fallback_reason,
            "cdpEndpoint": self.cdp_endpoint,
            "browserMode": self.browser_mode,
            "manualControlActive": self.manual_control_active,
            "capabilities": capabilities,
            "recentConsole": self.console_messages[-limit:],
            "recentNetwork": self.network_events[-limit:],
            "recentErrors": self.page_errors[-limit:],
            "counts": {
                "console": len(self.console_messages),
                "network": len(self.network_events),
                "errors": len(self.page_errors),
                "observedElements": len(
                    self.interactable_elements
                ),
            },
            "artifacts": artifacts,
        }

    def _flush_logs_to_artifacts(self) -> None:
        if not self.artifacts.session_dir:
            return
        try:
            if self.console_messages:
                path = self.artifacts.get_console_log_path()
                path.write_text(
                    json.dumps(self.console_messages, indent=2),
                    "utf-8",
                )
            if self.network_events:
                path = self.artifacts.get_network_log_path()
                path.write_text(
                    json.dumps(self.network_events, indent=2),
                    "utf-8",
                )
            if self.page_errors:
                path = self.artifacts.get_error_log_path()
                path.write_text(
                    json.dumps(self.page_errors, indent=2),
                    "utf-8",
                )
        except Exception:
            logger.warning(
                "Failed to flush logs to artifacts", exc_info=True
            )

    def record_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        if not self.artifacts.session_dir:
            return
        self.artifacts.append_event(event_type, payload or {})

    def write_artifact_json(
        self, filename: str, data: Any
    ) -> Path:
        return self.artifacts.write_json(filename, data)

    def write_artifact_text(
        self, filename: str, text: str
    ) -> Path:
        return self.artifacts.write_text(filename, text)
