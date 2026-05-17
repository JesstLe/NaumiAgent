# ruff: noqa: E501
"""SRI (Subresource Integrity) check module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def check_subresource_integrity(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Check external scripts and stylesheets for missing integrity attributes."""
    url = page.url
    findings: list[dict[str, Any]] = []

    script_analysis = await page.evaluate(
        """() => {
            const scripts = Array.from(document.querySelectorAll("script[src]"));
            return scripts.map((s) => ({
                src: s.src,
                integrity: s.integrity || null,
                crossorigin: s.crossOrigin || null,
                isExternal: s.src ? new URL(s.src).origin !== location.origin : false,
            }));
        }"""
    )

    for script in script_analysis:
        if not script.get("isExternal"):
            continue

        if not script.get("integrity"):
            findings.append(
                add_finding(
                    {
                        "category": "sri",
                        "severity": "medium",
                        "title": "External script without SRI",
                        "description": (
                            f"External script {script['src'][:100]} lacks integrity attribute"
                        ),
                        "url": url,
                        "evidence": {
                            "src": script["src"],
                            "integrity": None,
                            "crossorigin": script.get("crossorigin"),
                        },
                    }
                )
            )

        if not script.get("crossorigin"):
            findings.append(
                add_finding(
                    {
                        "category": "sri",
                        "severity": "low",
                        "title": "External script without crossorigin",
                        "description": f"External script {script['src'][:100]} lacks crossorigin attribute",
                        "url": url,
                        "evidence": {"src": script["src"], "crossorigin": None},
                    }
                )
            )

    link_analysis = await page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('link[rel="stylesheet"][href]'));
            return links.map((l) => ({
                href: l.href,
                integrity: l.integrity || null,
                isExternal: l.href ? new URL(l.href).origin !== location.origin : false,
            }));
        }"""
    )

    for link in link_analysis:
        if not link.get("isExternal"):
            continue
        if not link.get("integrity"):
            findings.append(
                add_finding(
                    {
                        "category": "sri",
                        "severity": "medium",
                        "title": "External stylesheet without SRI",
                        "description": (
                            f"External stylesheet {link['href'][:100]} lacks integrity attribute"
                        ),
                        "url": url,
                        "evidence": {"href": link["href"], "integrity": None},
                    }
                )
            )

    return {
        "category": "sri",
        "findings": findings,
        "scriptsChecked": len(script_analysis),
        "stylesheetsChecked": len(link_analysis),
    }
