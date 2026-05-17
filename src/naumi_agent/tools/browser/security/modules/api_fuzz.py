# ruff: noqa: E501
"""API fuzzing module."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


async def fuzz_api(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Fuzz discovered API endpoints with various payloads."""
    url = page.url
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    api_endpoints = await page.evaluate(
        """(pageOrigin) => {
            const found = new Set();
            const escaped = pageOrigin.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
            const apiPattern = new RegExp(escaped + "/api/[^'\\\\"\\\\s<>]+", "g");
            const html = document.documentElement.innerHTML;
            for (const m of html.matchAll(apiPattern)) found.add(m[0]);
            for (const a of document.querySelectorAll("a[href]")) {
                if (a.href.includes("/api/")) found.add(a.href);
            }
            return Array.from(found).slice(0, 15);
        }""",
        origin,
    )

    if not api_endpoints:
        return {
            "category": "api-fuzz",
            "findings": findings,
            "stats": {"endpoints": 0, "totalTests": 0, "errors": 0},
            "message": "No API endpoints discovered",
        }

    fuzz_payloads = [
        {"value": "A" * 10000, "name": "long-string", "severity": "medium"},
        {"value": -999999999, "name": "negative-overflow", "severity": "low"},
        {"value": 9999999999999999, "name": "integer-overflow", "severity": "low"},
        {"value": "../../etc/passwd", "name": "path-traversal", "severity": "high"},
        {"value": "{{7*7}}", "name": "template-injection", "severity": "high"},
        {"value": "${7*7}", "name": "expression-injection", "severity": "high"},
        {"value": "\\x00", "name": "null-byte", "severity": "medium"},
        {"value": "'", "name": "single-quote", "severity": "medium"},
        {"value": '"', "name": "double-quote", "severity": "medium"},
        {"value": "<script>alert(1)</script>", "name": "xss-basic", "severity": "high"},
        {"value": "'; DROP TABLE users--", "name": "sqli-basic", "severity": "high"},
    ]

    error_indicators = [
        r"stack trace",
        r"exception",
        r"error.*line \d+",
        r"traceback",
        r"internal server error",
        r"segfault",
        r"core dump",
        r"unhandled",
        r"undefined reference",
        r"null pointer",
        r"ORA-\d{5}",
        r"SQLSTATE\[",
        r"Fatal error",
    ]

    total_tests = 0
    error_count = 0

    for endpoint in api_endpoints[:10]:
        ep_parsed = urlparse(endpoint)
        params = list(ep_parsed.query.split("&")) if ep_parsed.query else []
        param_names = [p.split("=")[0] for p in params if "=" in p]

        if param_names:
            for param_name in param_names[:5]:
                for payload in fuzz_payloads:
                    total_tests += 1
                    try:
                        from urllib.parse import urlencode

                        test_qs = dict(p.split("=", 1) for p in params if "=" in p)
                        test_qs[param_name] = str(payload["value"])
                        test_url = f"{ep_parsed.scheme}://{ep_parsed.netloc}{ep_parsed.path}?{urlencode(test_qs)}"

                        result = await page.evaluate(
                            """async (fetchUrl) => {
                                try {
                                    const res = await fetch(fetchUrl, { signal: AbortSignal.timeout(5000) });
                                    const body = await res.text().catch(() => "");
                                    return { status: res.status, body: body.slice(0, 500) };
                                } catch { return null; }
                            }""",
                            test_url,
                        )

                        if not result:
                            continue
                        for pattern_str in error_indicators:
                            if re.search(pattern_str, result.get("body", ""), re.IGNORECASE) and result.get("status") == 500:
                                error_count += 1
                                findings.append(
                                    add_finding(
                                        {
                                            "category": "api-fuzz",
                                            "severity": payload["severity"],
                                            "title": f"API fuzz: {payload['name']} caused server error",
                                            "description": (
                                                f'Param "{param_name}" on {endpoint} with payload '
                                                f'"{payload["name"]}" returned HTTP 500'
                                            ),
                                            "url": endpoint,
                                            "evidence": {
                                                "param": param_name,
                                                "payload": payload["name"],
                                                "status": result["status"],
                                                "preview": result.get("body", "")[:200],
                                            },
                                        }
                                    )
                                )
                                break
                    except Exception:
                        continue

        # POST body fuzz
        total_tests += 1
        try:
            fuzz_body = {f"fuzz_{p['name']}": p["value"] for p in fuzz_payloads[:5]}
            result = await page.evaluate(
                """async ({ fetchUrl, fetchBody }) => {
                    try {
                        const res = await fetch(fetchUrl, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(fetchBody),
                            signal: AbortSignal.timeout(5000),
                        });
                        const body = await res.text().catch(() => "");
                        return { status: res.status, body: body.slice(0, 500) };
                    } catch { return null; }
                }""",
                {"fetchUrl": endpoint, "fetchBody": fuzz_body},
            )

            if result:
                for pattern_str in error_indicators:
                    if re.search(pattern_str, result.get("body", ""), re.IGNORECASE) and result.get("status") == 500:
                        error_count += 1
                        findings.append(
                            add_finding(
                                {
                                    "category": "api-fuzz",
                                    "severity": "high",
                                    "title": "API fuzz: POST body caused server error",
                                    "description": f"POST {endpoint} returned HTTP 500 with fuzzed body",
                                    "url": endpoint,
                                    "evidence": {
                                        "method": "POST",
                                        "status": result["status"],
                                        "preview": result.get("body", "")[:200],
                                    },
                                }
                            )
                        )
                        break
        except Exception:
            continue

    return {
        "category": "api-fuzz",
        "findings": findings,
        "stats": {
            "endpoints": len(api_endpoints),
            "totalTests": total_tests,
            "errors": error_count,
        },
    }
