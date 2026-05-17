# ruff: noqa: E501
"""IDOR (Insecure Direct Object Reference) test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_idor(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test API endpoints for IDOR by varying numeric IDs."""
    url = page.url
    from urllib.parse import urlencode, urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    api_urls = await page.evaluate(
        """(pageOrigin) => {
            const found = new Set();
            const escaped = pageOrigin.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
            const apiPattern = new RegExp(escaped + "/api/[^'\\\\"\\\\s<>]+", "g");
            const html = document.documentElement.innerHTML;
            for (const m of html.matchAll(apiPattern)) found.add(m[0]);
            for (const a of document.querySelectorAll("a[href]")) {
                if (a.href.includes("/api/")) found.add(a.href);
            }
            return Array.from(found).slice(0, 10);
        }""",
        origin,
    )

    id_range = [1, 2, 3, 4, 5]
    id_param = "id"

    for api_url in api_urls:
        try:
            parsed_api = urlparse(api_url)
            base_id = parsed_api.query and next(
                (v for k, v in __import__("urllib.parse").parse_qsl(parsed_api.query) if k == id_param),
                None,
            )

            for test_id in id_range:
                from urllib.parse import parse_qs, urlencode
                from urllib.parse import urlparse as _urlparse

                test_parsed = _urlparse(api_url)
                qs = parse_qs(test_parsed.query, keep_blank_values=True)
                if id_param in qs:
                    qs[id_param] = [str(test_id)]
                    new_query = urlencode({k: v[0] for k, v in qs.items()})
                else:
                    import re
                    path_parts = test_parsed.path.split("/")
                    last_num = next(
                        (i for i, p in enumerate(path_parts) if re.match(r"^\d+$", p)),
                        None,
                    )
                    if last_num is not None:
                        path_parts[last_num] = str(test_id)
                    new_query = test_parsed.query

                from urllib.parse import urlunparse as _urlunparse
                test_url = _urlunparse(
                    (
                        test_parsed.scheme,
                        test_parsed.netloc,
                        "/".join(path_parts) if last_num is not None else test_parsed.path,
                        test_parsed.params,
                        new_query,
                        test_parsed.fragment,
                    )
                )

                result = await page.evaluate(
                    """async (fetchUrl) => {
                        try {
                            const res = await fetch(fetchUrl, { signal: AbortSignal.timeout(5000) });
                            const body = await res.text().catch(() => "");
                            return {
                                status: res.status,
                                length: body.length,
                                hasData: body.length > 10 && res.ok,
                                preview: body.slice(0, 100),
                            };
                        } catch { return null; }
                    }""",
                    str(test_url),
                )

                if not result:
                    continue

                if (
                    result.get("status") == 200
                    and result.get("hasData")
                    and base_id
                    and str(test_id) != base_id
                ):
                    findings.append(
                        add_finding(
                            {
                                "category": "idor",
                                "severity": "high",
                                "title": "Potential IDOR vulnerability",
                                "description": (
                                    f"API endpoint {api_url} returns data for ID {test_id} "
                                    "without authorization check"
                                ),
                                "url": api_url,
                                "evidence": {
                                    "testId": test_id,
                                    "baseId": base_id,
                                    "status": result["status"],
                                    "responseLength": result["length"],
                                },
                            }
                        )
                    )
        except Exception:
            continue

    if not api_urls:
        findings.append(
            add_finding(
                {
                    "category": "idor",
                    "severity": "info",
                    "title": "No API endpoints discovered for IDOR testing",
                    "description": "No URL patterns matching /api/ with numeric IDs found",
                    "url": url,
                    "evidence": {},
                }
            )
        )

    return {"category": "idor", "findings": findings}
