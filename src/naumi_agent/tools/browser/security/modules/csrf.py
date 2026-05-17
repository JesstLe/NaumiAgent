# ruff: noqa: E501
"""CSRF (Cross-Site Request Forgery) detection module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def detect_csrf(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Detect forms missing CSRF tokens."""
    url = page.url
    findings: list[dict[str, Any]] = []

    forms = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll("form")).map((form) => {
                const inputs = Array.from(form.querySelectorAll("input, select, textarea"));
                return {
                    action: form.action || null,
                    method: (form.method || "GET").toUpperCase(),
                    inputNames: inputs.map((i) => i.name).filter(Boolean),
                    inputTypes: inputs.map((i) => i.type).filter(Boolean),
                    hasToken: inputs.some((i) => {
                        const n = (i.name || "").toLowerCase();
                        return n.includes("csrf") || n.includes("token")
                            || n.includes("_token") || n.includes("authenticity");
                    }),
                };
            });
        }"""
    )

    for form in forms:
        method = form.get("method", "GET")
        if method not in ("POST", "PUT", "DELETE"):
            continue

        if not form.get("hasToken"):
            findings.append(
                add_finding(
                    {
                        "category": "csrf",
                        "severity": "high",
                        "title": "Form missing CSRF token",
                        "description": f"POST form at {form.get('action') or url} has no CSRF token field",
                        "url": url,
                        "evidence": {
                            "action": form.get("action"),
                            "method": method,
                            "inputNames": form.get("inputNames"),
                        },
                    }
                )
            )

    has_double_submit = await page.evaluate(
        """() => {
            const cookies = document.cookie.split(";").map((c) => c.trim().toLowerCase());
            return cookies.some((c) => c.startsWith("csrf") || c.includes("token"));
        }"""
    )

    if (
        forms
        and not any(f.get("category") == "csrf" for f in findings)
        and not has_double_submit
    ):
        findings.append(
            add_finding(
                {
                    "category": "csrf",
                    "severity": "high",
                    "title": "No CSRF protection detected",
                    "description": (
                        "Page has state-changing forms but no CSRF token "
                        "or double-submit cookie pattern found"
                    ),
                    "url": url,
                    "evidence": {"formCount": len(forms)},
                }
            )
        )

    return {"category": "csrf", "findings": findings}
