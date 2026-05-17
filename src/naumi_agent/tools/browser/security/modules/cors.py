# ruff: noqa: E501
"""CORS misconfiguration audit module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def audit_cors(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for CORS misconfiguration by sending cross-origin requests."""
    url = page.url
    from urllib.parse import urlparse

    origin = urlparse(url).origin if hasattr(urlparse(url), "origin") else f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    findings: list[dict[str, Any]] = []
    test_origin = "https://evil.example.com"

    test_urls = [url, f"{origin}/api", f"{origin}/graphql"]
    for target in test_urls:
        try:
            response = await page.evaluate(
                """async (testUrl) => {
                    try {
                        const res = await fetch(testUrl, {
                            method: "OPTIONS",
                            headers: { "Origin": "https://evil.example.com" },
                            credentials: "include",
                        });
                        return {
                            status: res.status,
                            acao: res.headers.get("access-control-allow-origin"),
                            acac: res.headers.get("access-control-allow-credentials"),
                        };
                    } catch { return null; }
                }""",
                target,
            )

            if response is None:
                continue

            acao = response.get("acao")
            acac = response.get("acac")

            if acao == "*":
                findings.append(
                    add_finding(
                        {
                            "category": "cors",
                            "severity": "high",
                            "title": "Wildcard CORS",
                            "description": f"{target} returns Access-Control-Allow-Origin: *",
                            "url": target,
                            "evidence": response,
                        }
                    )
                )
            elif acao == test_origin and acac == "true":
                findings.append(
                    add_finding(
                        {
                            "category": "cors",
                            "severity": "critical",
                            "title": "CORS misconfiguration allows credential theft",
                            "description": f"{target} reflects arbitrary Origin with credentials enabled",
                            "url": target,
                            "evidence": response,
                        }
                    )
                )
        except Exception:
            continue

    return {"category": "cors", "findings": findings}
