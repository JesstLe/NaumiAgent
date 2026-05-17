# ruff: noqa: E501
"""XSS (Cross-Site Scripting) test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_xss(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test input fields for reflected XSS vulnerabilities."""
    url = page.url
    findings: list[dict[str, Any]] = []

    xss_payloads = [
        '<img src=x onerror="window.__xss_test=1">',
        '"><script>window.__xss_test=2</script>',
        "'-alert(1)-'",
        '<svg onload="window.__xss_test=3">',
        "javascript:window.__xss_test=4",
        '{{constructor.constructor("return window")().__xss_test=5}}',
    ]

    targets = await page.evaluate(
        """() => {
            const inputs = document.querySelectorAll(
                "input[type='text'], input[type='search'], input:not([type]), textarea"
            );
            return Array.from(inputs).slice(0, 10).map((el, i) => ({
                index: i,
                tag: el.tagName,
                type: el.type || "text",
                name: el.name || "",
                id: el.id || "",
            }));
        }"""
    )

    if not targets:
        return {"category": "xss", "findings": findings, "message": "No input fields found to test"}

    for target in targets:
        selector = f"#{target['id']}" if target.get("id") else (
            f"[name=\"{target['name']}\"]" if target.get("name") else None
        )
        if not selector:
            continue

        for payload in xss_payloads:
            try:
                locator = page.locator(selector).first()
                visible = await locator.is_visible()
                if not visible:
                    continue

                await locator.fill(payload)
                form = await locator.evaluate(
                    """(el) => {
                        const f = el.closest("form");
                        return f ? { action: f.action, method: f.method } : null;
                    }"""
                )

                if form:
                    await locator.press("Enter")
                    await page.wait_for_timeout(1500)

                reflected = await page.evaluate("() => window.__xss_test")
                if reflected:
                    findings.append(
                        add_finding(
                            {
                                "category": "xss",
                                "severity": "critical",
                                "title": "XSS vulnerability detected",
                                "description": f'Payload reflected and executed in input field "{selector}"',
                                "url": page.url,
                                "evidence": {
                                    "selector": selector,
                                    "payload": payload,
                                    "form": form,
                                    "reflected": reflected,
                                },
                            }
                        )
                    )
                    await page.evaluate("() => { delete window.__xss_test; }")
                    break

                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            except Exception:
                continue

    return {"category": "xss", "findings": findings}
