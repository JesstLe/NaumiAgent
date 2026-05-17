# ruff: noqa: E501
"""Clickjacking detection module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_clickjacking(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for clickjacking by checking framing protections and iframe behavior."""
    url = page.url
    findings: list[dict[str, Any]] = []

    response = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
    headers_raw: dict[str, str] = {}
    if response:
        hdrs = await response.all_headers()
        headers_raw = {k.lower(): v for k, v in hdrs.items()}

    xfo = headers_raw.get("x-frame-options")
    csp = headers_raw.get("content-security-policy", "")
    csp_frame = "frame-ancestors" in csp

    if not xfo and not csp_frame:
        findings.append(
            add_finding(
                {
                    "category": "clickjacking",
                    "severity": "high",
                    "title": "Page can be framed (clickjacking)",
                    "description": (
                        "Neither X-Frame-Options nor CSP frame-ancestors "
                        "prevents embedding in iframe"
                    ),
                    "url": url,
                    "evidence": {
                        "xfo": xfo,
                        "cspFrameAncestors": "present" if csp_frame else "absent",
                    },
                }
            )
        )

    iframe_result = await page.evaluate(
        """(pageUrl) => {
            return new Promise((resolve) => {
                const iframe = document.createElement("iframe");
                iframe.style.cssText = "position:fixed;top:-9999px;width:100px;height:100px;";
                iframe.src = pageUrl;
                iframe.onload = () => {
                    try {
                        const doc = iframe.contentDocument || iframe.contentWindow?.document;
                        resolve({
                            loaded: true,
                            accessible: !!doc,
                            sameOrigin: doc?.title !== undefined,
                        });
                    } catch {
                        resolve({ loaded: true, accessible: false, crossOrigin: true });
                    }
                    iframe.remove();
                };
                iframe.onerror = () => { resolve({ loaded: false }); iframe.remove(); };
                document.body.appendChild(iframe);
                setTimeout(() => { iframe.remove(); resolve({ timeout: true }); }, 5000);
            });
        }""",
        url,
    )

    if iframe_result.get("loaded") and iframe_result.get("accessible"):
        findings.append(
            add_finding(
                {
                    "category": "clickjacking",
                    "severity": "high",
                    "title": "Page is iframeable",
                    "description": "Target page loaded successfully inside an iframe on the same origin",
                    "url": url,
                    "evidence": iframe_result,
                }
            )
        )

    external_iframe = await page.evaluate(
        """(pageUrl) => {
            return new Promise((resolve) => {
                const iframe = document.createElement("iframe");
                iframe.style.cssText = "position:fixed;top:-9999px;width:100px;height:100px;";
                iframe.src = pageUrl;
                const start = Date.now();
                iframe.onload = () => {
                    resolve({ loaded: true, elapsed: Date.now() - start });
                    iframe.remove();
                };
                iframe.onerror = () => {
                    resolve({ loaded: false, elapsed: Date.now() - start });
                    iframe.remove();
                };
                document.body.appendChild(iframe);
                setTimeout(() => { iframe.remove(); resolve({ timeout: true }); }, 5000);
            });
        }""",
        url,
    )

    if external_iframe.get("loaded"):
        findings.append(
            add_finding(
                {
                    "category": "clickjacking",
                    "severity": "medium",
                    "title": "Page loads in cross-origin iframe",
                    "description": (
                        "Page content loaded when embedded as iframe — "
                        "may be vulnerable to clickjacking attacks"
                    ),
                    "url": url,
                    "evidence": {"elapsed": external_iframe.get("elapsed")},
                }
            )
        )

    return {"category": "clickjacking", "findings": findings}
