# ruff: noqa: E501
"""Race condition test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_race_condition(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for race conditions by sending concurrent requests."""
    url = page.url
    findings: list[dict[str, Any]] = []
    target_url = url
    method = "POST"
    concurrency = 10

    race_results = await page.evaluate(
        """async ({ fetchUrl, fetchMethod, n }) => {
            const opts = {
                method: fetchMethod,
                headers: { "Content-Type": "application/json" },
                credentials: "include",
            };

            const promises = [];
            for (let i = 0; i < n; i++) {
                promises.push(fetch(fetchUrl, opts).then(async (r) => {
                    const body = await r.text().catch(() => "");
                    return { status: r.status, ok: r.ok, bodyLength: body.length };
                }).catch(() => ({ status: 0, error: true })));
            }

            const start = performance.now();
            const results = await Promise.all(promises);
            const elapsed = performance.now() - start;

            const successes = results.filter((r) => r.ok && !r.error);
            const bodies = successes.map((r) => r.bodyLength);
            const uniqueLengths = new Set(bodies).size;
            const firstBody = successes[0]?.bodyLength || 0;
            const allSame = successes.every((r) => r.bodyLength === firstBody);

            return {
                results: successes.map((r) => ({ status: r.status, ok: r.ok, bodyLength: r.bodyLength })),
                elapsed,
                successes: successes.length,
                failures: results.length - successes.length,
                total: n,
                uniqueBodyLengths: uniqueLengths,
                allSameBody: allSame,
            };
        }""",
        {"fetchUrl": target_url, "fetchMethod": method, "n": concurrency},
    )

    if race_results.get("successes", 0) > 1:
        if not race_results.get("allSameBody") or race_results.get("uniqueBodyLengths", 1) > 1:
            findings.append(
                add_finding(
                    {
                        "category": "race-condition",
                        "severity": "high",
                        "title": "Race condition — inconsistent responses",
                        "description": (
                            f"{race_results['successes']}/{race_results['total']} concurrent "
                            f"{method} requests returned {race_results.get('uniqueBodyLengths', 1)} "
                            "distinct response sizes"
                        ),
                        "url": target_url,
                        "evidence": race_results,
                    }
                )
            )
        else:
            findings.append(
                add_finding(
                    {
                        "category": "race-condition",
                        "severity": "info",
                        "title": "Race condition test completed",
                        "description": (
                            f"{race_results['successes']}/{race_results['total']} concurrent "
                            f"{method} requests returned identical responses"
                        ),
                        "url": target_url,
                        "evidence": race_results,
                    }
                )
            )
    else:
        findings.append(
            add_finding(
                {
                    "category": "race-condition",
                    "severity": "info",
                    "title": "Race condition test completed",
                    "description": (
                        f"{race_results.get('successes', 0)}/{race_results.get('total', concurrency)} "
                        f"succeeded in {round(race_results.get('elapsed', 0))}ms"
                    ),
                    "url": target_url,
                    "evidence": race_results,
                }
            )
        )

    return {"category": "race-condition", "findings": findings}
