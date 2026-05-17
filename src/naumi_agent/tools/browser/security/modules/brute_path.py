# ruff: noqa: E501
"""Path brute-force discovery module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def brute_path(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Discover common paths and endpoints via client-side fetch probing."""
    url = page.url
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    default_paths = [
        "/admin", "/login", "/dashboard", "/api", "/api/v1", "/api/v2",
        "/console", "/manager", "/control", "/panel", "/cpanel",
        "/backup", "/db", "/database", "/dump", "/export",
        "/upload", "/uploads", "/files", "/media", "/static",
        "/test", "/dev", "/staging", "/demo", "/beta",
        "/.git", "/.svn", "/.hg",
        "/wp-admin", "/wp-login.php", "/wp-content",
        "/administrator", "/phpmyadmin", "/adminer",
        "/cgi-bin", "/bin", "/conf", "/config",
        "/swagger-ui", "/graphql", "/graphiql",
        "/jenkins", "/ci", "/gitlab", "/bitbucket",
        "/solr", "/elastic", "/kibana",
        "/prometheus", "/grafana",
        "/trace", "/debug", "/profiler",
    ]

    results = await page.evaluate(
        """async (pathList) => {
            const found = [];
            for (const p of pathList) {
                try {
                    const res = await fetch(p, {
                        method: "GET",
                        signal: AbortSignal.timeout(2000),
                        redirect: "manual",
                    });
                    if (res.status !== 0 && res.status !== 404) {
                        found.push({ path: p, status: res.status });
                    }
                } catch {}
            }
            return found;
        }""",
        default_paths,
    )

    def severity_map(path: str, status: int) -> str:
        if any(kw in path for kw in ("admin", "phpmyadmin", "git")):
            return "high"
        if any(kw in path for kw in ("config", "backup", "dump")):
            return "high"
        if any(kw in path for kw in ("api", "upload")):
            return "medium"
        if status in (401, 403):
            return "info"
        return "low"

    for result in results:
        path = result["path"]
        status = result["status"]
        findings.append(
            add_finding(
                {
                    "category": "path-discovery",
                    "severity": severity_map(path, status),
                    "title": f"Discovered: {path}",
                    "description": f"{origin}{path} returned HTTP {status}",
                    "url": f"{origin}{path}",
                    "evidence": {"path": path, "status": status},
                }
            )
        )

    return {"category": "path-discovery", "findings": findings}
