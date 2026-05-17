# ruff: noqa: E501
"""Cookie security audit module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def audit_cookies(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Check cookies for security flags (HttpOnly, Secure, SameSite)."""
    context = page.context
    cookies = await context.cookies()
    url = page.url
    findings: list[dict[str, Any]] = []

    for cookie in cookies:
        name_lower = cookie.get("name", "").lower()
        is_sensitive_name = any(
            kw in name_lower for kw in ("session", "token", "auth")
        )

        if not cookie.get("httpOnly") and is_sensitive_name:
            findings.append(
                add_finding(
                    {
                        "category": "cookies",
                        "severity": "high",
                        "title": "Sensitive cookie without HttpOnly",
                        "description": (
                            f'Cookie "{cookie["name"]}" appears to be session/auth related '
                            "but lacks HttpOnly flag, accessible to JavaScript"
                        ),
                        "url": url,
                        "evidence": {
                            "name": cookie["name"],
                            "domain": cookie.get("domain"),
                            "httpOnly": cookie.get("httpOnly"),
                        },
                    }
                )
            )

        if not cookie.get("secure") and "https" in cookie.get("domain", ""):
            findings.append(
                add_finding(
                    {
                        "category": "cookies",
                        "severity": "medium",
                        "title": "Cookie without Secure flag",
                        "description": f'Cookie "{cookie["name"]}" will be sent over HTTP connections',
                        "url": url,
                        "evidence": {
                            "name": cookie["name"],
                            "domain": cookie.get("domain"),
                            "secure": cookie.get("secure"),
                        },
                    }
                )
            )

        same_site = cookie.get("sameSite")
        if not same_site or same_site == "None":
            findings.append(
                add_finding(
                    {
                        "category": "cookies",
                        "severity": "medium",
                        "title": "Cookie with lax SameSite",
                        "description": (
                            f'Cookie "{cookie["name"]}" has SameSite={same_site or "unset"}, '
                            "vulnerable to CSRF"
                        ),
                        "url": url,
                        "evidence": {
                            "name": cookie["name"],
                            "domain": cookie.get("domain"),
                            "sameSite": same_site,
                        },
                    }
                )
            )

    return {"category": "cookies", "findings": findings, "totalCookies": len(cookies)}
