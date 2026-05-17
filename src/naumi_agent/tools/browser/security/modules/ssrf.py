# ruff: noqa: E501
"""SSRF (Server-Side Request Forgery) test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_ssrf(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for SSRF via input fields and browser-side fetch probes."""
    url = page.url
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    ssrf_targets = [
        "http://127.0.0.1",
        "http://localhost",
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/latest/user-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[::1]",
        "http://0.0.0.0",
    ]

    input_fields = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll(
                "input[type='url'], input[type='text'][name*='url'], "
                + "input[type='text'][name*='link'], input[type='text'][name*='fetch'], "
                + "input[type='text'][name*='site']"
            )).slice(0, 5).map((el) => ({
                name: el.name || "",
                id: el.id || "",
                type: el.type,
            }));
        }"""
    )

    if input_fields:
        for field in input_fields:
            selector = f"#{field['id']}" if field.get("id") else f"[name=\"{field['name']}\"]"
            for target in ssrf_targets:
                try:
                    locator = page.locator(selector).first()
                    visible = await locator.is_visible()
                    if not visible:
                        continue

                    await locator.fill(target)
                    await locator.press("Enter")
                    await page.wait_for_timeout(2000)

                    content = await page.inner_text("body")
                    leaked_keywords = [
                        "ami-", "instance-id", "ami-id", "privateIp", "meta-data", "computeMetadata",
                    ]
                    leaked = any(kw in content for kw in leaked_keywords)

                    if leaked:
                        findings.append(
                            add_finding(
                                {
                                    "category": "ssrf",
                                    "severity": "critical",
                                    "title": "SSRF vulnerability detected",
                                    "description": (
                                        f'Internal resource response leaked after injecting '
                                        f'"{target}" into "{selector}"'
                                    ),
                                    "url": page.url,
                                    "evidence": {"selector": selector, "ssrfTarget": target},
                                }
                            )
                        )
                        break
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    continue

    probe_results = await page.evaluate(
        """async (ssrfTargets) => {
            const baselineStart = Date.now();
            try {
                await fetch("http://192.0.2.1", {
                    signal: AbortSignal.timeout(3000),
                    mode: "no-cors",
                });
            } catch {}
            const baselineElapsed = Date.now() - baselineStart;

            const results = [];
            for (const target of ssrfTargets) {
                const tStart = Date.now();
                try {
                    const res = await fetch(target, {
                        signal: AbortSignal.timeout(5000),
                        mode: "no-cors",
                    });
                    const elapsed = Date.now() - tStart;
                    results.push({
                        target,
                        status: res.status,
                        type: res.type,
                        elapsed,
                        fasterThanBaseline: elapsed < baselineElapsed * 0.5,
                    });
                } catch (err) {
                    const elapsed = Date.now() - tStart;
                    results.push({
                        target,
                        error: (err.message || "").slice(0, 100),
                        elapsed,
                    });
                }
            }
            results.baselineElapsed = baselineElapsed;
            return results;
        }""",
        ssrf_targets,
    )

    baseline_elapsed = probe_results.get("baselineElapsed", 3000) if isinstance(probe_results, dict) else 3000
    probes = [r for r in (probe_results if isinstance(probe_results, list) else [])]

    for probe in probes:
        if not probe.get("target"):
            continue
        if probe.get("type") == "opaque":
            continue
        elapsed = probe.get("elapsed", 0)
        if elapsed and elapsed < baseline_elapsed * 0.3 and elapsed < 500:
            findings.append(
                add_finding(
                    {
                        "category": "ssrf",
                        "severity": "high",
                        "title": "Internal endpoint may be reachable",
                        "description": (
                            f"{probe['target']} responded in {elapsed}ms "
                            f"(baseline: {baseline_elapsed}ms)"
                        ),
                        "url": origin,
                        "evidence": {
                            **probe,
                            "baselineElapsed": baseline_elapsed,
                            "note": (
                                "Browser-side SSRF testing is limited by CORS; "
                                "for full coverage use server-side proxy"
                            ),
                        },
                    }
                )
            )

    if not findings and not input_fields:
        findings.append(
            add_finding(
                {
                    "category": "ssrf",
                    "severity": "info",
                    "title": "SSRF test completed (limited scope)",
                    "description": (
                        "No URL-accepting input fields found. "
                        "Browser-side fetch probes blocked by CORS (expected)."
                    ),
                    "url": url,
                    "evidence": {
                        "note": (
                            "Browser-side SSRF testing is inherently limited. "
                            "Use dedicated SSRF tools for full coverage."
                        ),
                    },
                }
            )
        )

    return {"category": "ssrf", "findings": findings}
