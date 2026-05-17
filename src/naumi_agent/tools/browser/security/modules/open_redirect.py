# ruff: noqa: E501
"""Open redirect detection module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_open_redirect(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for open redirect vulnerabilities via common parameter names."""
    url = page.url
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    redirect_params = [
        "redirect", "url", "next", "return", "returnUrl", "returnTo",
        "goto", "continue", "destination", "redir", "redirect_uri",
        "target", "forward",
    ]

    links = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll("a[href]")).slice(0, 30).map((a) => ({
                href: a.href,
                hasRedirectParam: /[?&](redirect|url|next|return|goto|continue|destination|redir|target|forward)=/i
                    .test(a.href),
            }));
        }"""
    )

    evil_url = "https://evil.example.com/phishing"
    test_url_base = f"{origin}/test?"

    for param in redirect_params:
        try:
            probe_url = f"{test_url_base}{param}={evil_url}"
            result = await page.evaluate(
                """async (fetchUrl) => {
                    try {
                        const res = await fetch(fetchUrl, {
                            redirect: "manual",
                            signal: AbortSignal.timeout(3000),
                        });
                        return {
                            status: res.status,
                            location: res.headers.get("location"),
                            type: res.type,
                        };
                    } catch { return null; }
                }""",
                probe_url,
            )

            if (
                result
                and 300 <= result.get("status", 0) < 400
                and "evil.example.com" in (result.get("location") or "")
            ):
                findings.append(
                    add_finding(
                        {
                            "category": "open-redirect",
                            "severity": "medium",
                            "title": "Open redirect via parameter",
                            "description": f'Parameter "{param}" causes redirect to external URL',
                            "url": probe_url,
                            "evidence": {
                                "param": param,
                                "status": result["status"],
                                "location": result["location"],
                            },
                        }
                    )
                )
        except Exception:
            continue

    for link in links:
        if link.get("hasRedirectParam") and origin in link.get("href", ""):
            findings.append(
                add_finding(
                    {
                        "category": "open-redirect",
                        "severity": "info",
                        "title": "Link with redirect parameter",
                        "description": f"Page contains link with redirect parameter: {link['href'][:100]}",
                        "url": url,
                        "evidence": {"href": link["href"]},
                    }
                )
            )

    return {"category": "open-redirect", "findings": findings}
