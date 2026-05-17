# ruff: noqa: E501
"""SSTI (Server-Side Template Injection) test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_ssti(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test input fields and URL params for template injection."""
    url = page.url
    findings: list[dict[str, Any]] = []

    ssti_payloads = [
        {"payload": "{{7*7}}", "expected": "49", "engine": "Jinja2/Twig"},
        {"payload": "${7*7}", "expected": "49", "engine": "EL/SpEL"},
        {"payload": "#{7*7}", "expected": "49", "engine": "Thymeleaf"},
        {"payload": "<%= 7*7 %>", "expected": "49", "engine": "ERB"},
        {"payload": "{{7*'7'}}", "expected": "7777777", "engine": "Jinja2 (multiply)"},
        {"payload": "{% debug %}", "expected": None, "engine": "Django"},
        {"payload": "{{config}}", "expected": None, "engine": "Jinja2/Flask"},
        {"payload": "{{self.__class__.__mro__}}", "expected": None, "engine": "Python/Jinja2"},
    ]

    targets = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll(
                "input[type='text'], input[type='search'], input:not([type]), textarea"
            )).slice(0, 5).map((el) => ({ name: el.name || "", id: el.id || "" }));
        }"""
    )

    if not targets:
        params_js = await page.evaluate(
            """() => {
                const urlParams = new URL(location.href).searchParams;
                const params = [];
                for (const [param] of urlParams) {
                    params.push({ name: param, id: "", isUrlParam: true });
                }
                return params;
            }"""
        )
        targets = params_js

    if not targets:
        return {"category": "ssti", "findings": findings, "message": "No input fields or URL parameters found"}

    import time

    for target in targets:
        try:
            calibrate = f"SSTI_CALIBRATE_{int(time.time() * 1000)}"
            if target.get("isUrlParam"):
                from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

                parsed = urlparse(page.url)
                qs = parse_qs(parsed.query, keep_blank_values=True)
                qs[target["name"]] = [calibrate]
                test_url = urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode({k: v[0] for k, v in qs.items()}), parsed.fragment)
                )
                await page.goto(test_url, wait_until="domcontentloaded", timeout=10000)
            else:
                selector = f"#{target['id']}" if target.get("id") else f"[name=\"{target['name']}\"]"
                locator = page.locator(selector).first()
                visible = await locator.is_visible()
                if not visible:
                    continue
                await locator.fill(calibrate)
                await locator.press("Enter")
                await page.wait_for_timeout(2000)

            _baseline_content = await page.inner_text("body")
        except Exception:
            continue

        for payload_spec in ssti_payloads:
            payload = payload_spec["payload"]
            expected = payload_spec["expected"]
            engine = payload_spec["engine"]

            try:
                if target.get("isUrlParam"):
                    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

                    parsed = urlparse(page.url)
                    qs = parse_qs(parsed.query, keep_blank_values=True)
                    qs[target["name"]] = [payload]
                    test_url = urlunparse(
                        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode({k: v[0] for k, v in qs.items()}), parsed.fragment)
                    )
                    await page.goto(test_url, wait_until="domcontentloaded", timeout=10000)
                else:
                    selector = f"#{target['id']}" if target.get("id") else f"[name=\"{target['name']}\"]"
                    locator = page.locator(selector).first()
                    visible = await locator.is_visible()
                    if not visible:
                        continue
                    await locator.fill(payload)
                    await locator.press("Enter")
                    await page.wait_for_timeout(2000)

                content = await page.inner_text("body")

                has_rendered = expected and expected in content
                has_canonical = payload in content

                if has_rendered and not has_canonical:
                    findings.append(
                        add_finding(
                            {
                                "category": "ssti",
                                "severity": "critical",
                                "title": f"Template injection ({engine})",
                                "description": (
                                    f'SSTI payload "{payload}" was evaluated '
                                    f'(output "{expected}" found, confirming server-side rendering)'
                                ),
                                "url": page.url,
                                "evidence": {
                                    "payload": payload,
                                    "engine": engine,
                                    "target": target.get("name") or target.get("id"),
                                    "isUrlParam": target.get("isUrlParam", False),
                                    "rendered": expected,
                                },
                            }
                        )
                    )
                    break

                if not expected:
                    suspicious = any(kw in content for kw in ("Debug", "config", "__class__"))
                    not_just_reflected = payload not in content
                    if suspicious and not_just_reflected:
                        findings.append(
                            add_finding(
                                {
                                    "category": "ssti",
                                    "severity": "high",
                                    "title": f"Possible template injection ({engine})",
                                    "description": (
                                        f'SSTI payload "{payload}" may have been evaluated '
                                        "— suspicious output detected"
                                    ),
                                    "url": page.url,
                                    "evidence": {
                                        "payload": payload,
                                        "engine": engine,
                                        "target": target.get("name") or target.get("id"),
                                        "isUrlParam": target.get("isUrlParam", False),
                                    },
                                }
                            )
                        )
                        break

                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            except Exception:
                continue

    return {"category": "ssti", "findings": findings}
