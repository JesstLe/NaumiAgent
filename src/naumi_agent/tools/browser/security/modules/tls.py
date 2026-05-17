# ruff: noqa: E501
"""TLS/HTTPS audit module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def audit_tls(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Audit TLS configuration including protocol, HSTS, and mixed content."""
    url = page.url
    findings: list[dict[str, Any]] = []

    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        findings.append(
            add_finding(
                {
                    "category": "tls",
                    "severity": "high",
                    "title": "Page not served over HTTPS",
                    "description": "Connection is not encrypted — all data transmitted in plaintext",
                    "url": url,
                    "evidence": {"protocol": parsed.scheme},
                }
            )
        )
        return {"category": "tls", "findings": findings}

    tls_info = await page.evaluate(
        """async () => {
            const info = {};
            try {
                const nav = performance.getEntriesByType("navigation");
                const entry = nav[0];
                if (entry) {
                    info.nextHopProtocol = entry.nextHopProtocol || null;
                    info.transferSize = entry.transferSize;
                }
            } catch {}
            info.isSecureContext = window.isSecureContext;
            return info;
        }"""
    )

    if not tls_info.get("isSecureContext"):
        findings.append(
            add_finding(
                {
                    "category": "tls",
                    "severity": "high",
                    "title": "Not a secure context",
                    "description": "window.isSecureContext is false despite HTTPS",
                    "url": url,
                    "evidence": tls_info,
                }
            )
        )

    proto = tls_info.get("nextHopProtocol")
    if proto == "http/1.1":
        findings.append(
            add_finding(
                {
                    "category": "tls",
                    "severity": "low",
                    "title": "Using HTTP/1.1",
                    "description": "Consider upgrading to HTTP/2 or HTTP/3",
                    "url": url,
                    "evidence": {"protocol": proto},
                }
            )
        )

    security_headers = await page.evaluate(
        """async (pageUrl) => {
            try {
                const res = await fetch(pageUrl, { method: "GET" });
                return {
                    hsts: res.headers.get("strict-transport-security"),
                    expectCt: res.headers.get("expect-ct"),
                };
            } catch { return null; }
        }""",
        url,
    )

    if security_headers:
        hsts = security_headers.get("hsts")
        if not hsts:
            findings.append(
                add_finding(
                    {
                        "category": "tls",
                        "severity": "high",
                        "title": "Missing HSTS header",
                        "description": "Strict-Transport-Security not set",
                        "url": url,
                        "evidence": {"hsts": None},
                    }
                )
            )
        else:
            import re

            max_age_match = re.search(r"max-age=(\d+)", hsts)
            if max_age_match and int(max_age_match.group(1)) < 63072000:
                findings.append(
                    add_finding(
                        {
                            "category": "tls",
                            "severity": "low",
                            "title": "HSTS max-age below recommended",
                            "description": "HSTS max-age should be at least 63072000 (2 years)",
                            "url": url,
                            "evidence": {"hsts": hsts},
                        }
                    )
                )
            if "includeSubDomains" not in hsts:
                findings.append(
                    add_finding(
                        {
                            "category": "tls",
                            "severity": "low",
                            "title": "HSTS missing includeSubDomains",
                            "description": "HSTS does not include subdomains",
                            "url": url,
                            "evidence": {"hsts": hsts},
                        }
                    )
                )
            if "preload" not in hsts:
                findings.append(
                    add_finding(
                        {
                            "category": "tls",
                            "severity": "info",
                            "title": "HSTS not preloaded",
                            "description": "HSTS preload directive missing",
                            "url": url,
                            "evidence": {"hsts": hsts},
                        }
                    )
                )

    mixed_content = await page.evaluate(
        """() => {
            const issues = [];
            const resources = document.querySelectorAll(
                "img[src], script[src], link[href], iframe[src], video[src], audio[src]"
            );
            for (const el of resources) {
                const src = el.src || el.href;
                if (src && src.startsWith("http://")) {
                    issues.push({ tag: el.tagName, src: src.slice(0, 100) });
                }
            }
            return issues;
        }"""
    )

    for item in mixed_content:
        findings.append(
            add_finding(
                {
                    "category": "tls",
                    "severity": "medium",
                    "title": "Mixed content detected",
                    "description": f"<{item['tag']}> loads HTTP resource on HTTPS page",
                    "url": url,
                    "evidence": item,
                }
            )
        )

    return {"category": "tls", "findings": findings}
