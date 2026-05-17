# ruff: noqa: E501
"""Information disclosure scan module."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


async def scan_info_leaks(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Scan for exposed sensitive files and leaked secrets in page source."""
    url = page.url
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[dict[str, Any]] = []

    leak_paths = [
        ("/.env", "critical", "Environment file exposed"),
        ("/.git/config", "critical", "Git repository exposed"),
        ("/.git/HEAD", "critical", "Git HEAD exposed"),
        ("/wp-config.php", "critical", "WordPress config exposed"),
        ("/config.json", "high", "Config JSON exposed"),
        ("/package.json", "low", "Package metadata exposed"),
        ("/robots.txt", "info", "Robots.txt"),
        ("/sitemap.xml", "info", "Sitemap"),
        ("/.well-known/security.txt", "info", "Security policy"),
        ("/server-status", "medium", "Apache server status"),
        ("/server-info", "medium", "Apache server info"),
        ("/phpinfo.php", "high", "PHP info page"),
        ("/debug", "high", "Debug endpoint"),
        ("/api/docs", "low", "API docs"),
        ("/swagger.json", "low", "Swagger spec"),
        ("/graphql", "low", "GraphQL endpoint"),
        ("/.DS_Store", "medium", "macOS directory listing"),
        ("/WEB-INF/web.xml", "high", "Java web config"),
        ("/actuator/health", "low", "Spring Actuator health"),
        ("/actuator/env", "high", "Spring Actuator env (may leak secrets)"),
    ]

    checks_js = [
        {"path": p, "severity": s, "desc": d}
        for p, s, d in leak_paths
    ]

    results = await page.evaluate(
        """async (checks) => {
            const outcomes = [];
            for (const check of checks) {
                try {
                    const res = await fetch(check.path, {
                        method: "GET",
                        signal: AbortSignal.timeout(3000),
                    });
                    if (res.ok && res.status !== 404) {
                        const text = await res.text().catch(() => "");
                        outcomes.push({
                            path: check.path,
                            status: res.status,
                            severity: check.severity,
                            desc: check.desc,
                            preview: text.slice(0, 200),
                        });
                    }
                } catch {}
            }
            return outcomes;
        }""",
        checks_js,
    )

    for result in results:
        findings.append(
            add_finding(
                {
                    "category": "info-leak",
                    "severity": result["severity"],
                    "title": result["desc"],
                    "description": f"Accessible at {origin}{result['path']} (HTTP {result['status']})",
                    "url": f"{origin}{result['path']}",
                    "evidence": {
                        "path": result["path"],
                        "status": result["status"],
                        "preview": result.get("preview", ""),
                    },
                }
            )
        )

    # Check page source for sensitive patterns
    page_source = await page.content()
    sensitive_patterns = [
        (r"API[_-]?KEY\s*[:=]\s*['\"]([\w-]{20,})['\"]", "API key in source", "critical"),
        (r"secret[_-]?key\s*[:=]\s*['\"]([\w-]{10,})['\"]", "Secret key in source", "critical"),
        (r"password\s*[:=]\s*['\"](.+?)['\"]", "Password in source", "critical"),
        (r"aws[_-]?access[_-]?key[_-]?id\s*[:=]\s*['\"]([\w]{20})['\"]", "AWS key in source", "critical"),
        (r"Bearer\s+[\w-]+\.[\w-]+\.[\w-]+", "JWT token in source", "high"),
        (r"mongodb://\S+:\S+@", "MongoDB connection string", "critical"),
        (r"mysql://\S+:\S+@", "MySQL connection string", "critical"),
        (r"postgresql://\S+:\S+@", "PostgreSQL connection string", "critical"),
        (r"sourceMappingURL=(.+?)\.map", "Source map exposed", "medium"),
        (r"192\.168\.\d+\.\d+", "Internal IP in source", "low"),
        (r"10\.\d+\.\d+\.\d+", "Internal IP in source", "low"),
    ]

    for pattern_str, name, severity in sensitive_patterns:
        match = re.search(pattern_str, page_source, re.IGNORECASE)
        if match:
            findings.append(
                add_finding(
                    {
                        "category": "info-leak",
                        "severity": severity,
                        "title": name,
                        "description": "Sensitive pattern found in page source",
                        "url": url,
                        "evidence": {"pattern": name, "match": match.group(0)[:100]},
                    }
                )
            )

    return {"category": "info-leak", "findings": findings}
