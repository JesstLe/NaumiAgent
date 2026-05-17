# ruff: noqa: E501
"""Security headers audit module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def audit_security_headers(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Check for missing or misconfigured security response headers."""
    url = page.url
    response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    headers_raw: dict[str, str] = {}
    if response:
        hdrs = await response.all_headers()
        headers_raw = {k.lower(): v for k, v in hdrs.items()}

    checks = [
        ("content-security-policy", "high", "Missing CSP allows XSS and data injection"),
        ("strict-transport-security", "high", "Missing HSTS allows protocol downgrade"),
        ("x-frame-options", "medium", "Missing X-Frame-Options enables clickjacking"),
        ("x-content-type-options", "medium", "Missing nosniff allows MIME sniffing"),
        ("x-xss-protection", "low", "Missing XSS protection header"),
        ("referrer-policy", "low", "Missing Referrer-Policy may leak URLs"),
        ("permissions-policy", "info", "Missing Permissions-Policy (formerly Feature-Policy)"),
        ("cross-origin-opener-policy", "medium", "Missing COOP allows cross-origin attacks"),
        ("cross-origin-resource-policy", "medium", "Missing CORP allows cross-origin resource leaks"),
    ]

    findings: list[dict[str, Any]] = []
    for name, severity, description in checks:
        value = headers_raw.get(name)
        if not value:
            findings.append(
                add_finding(
                    {
                        "category": "security-headers",
                        "severity": severity,
                        "title": f"Missing {name}",
                        "description": description,
                        "url": url,
                        "evidence": {"header": name, "present": False},
                    }
                )
            )
        elif name == "strict-transport-security":
            import re

            max_age_match = re.search(r"max-age=(\d+)", value)
            if max_age_match and int(max_age_match.group(1)) < 2592000:
                findings.append(
                    add_finding(
                        {
                            "category": "security-headers",
                            "severity": "low",
                            "title": "Weak HSTS max-age",
                            "description": "HSTS max-age should be at least 2592000 (30 days)",
                            "url": url,
                            "evidence": {"header": name, "value": value},
                        }
                    )
                )
        elif name == "x-frame-options" and value.lower() not in ("deny", "sameorigin"):
            findings.append(
                add_finding(
                    {
                        "category": "security-headers",
                        "severity": "medium",
                        "title": "Weak X-Frame-Options value",
                        "description": f"X-Frame-Options should be DENY or SAMEORIGIN, got: {value}",
                        "url": url,
                        "evidence": {"header": name, "value": value},
                    }
                )
            )

    return {"category": "security-headers", "findings": findings, "url": url}
