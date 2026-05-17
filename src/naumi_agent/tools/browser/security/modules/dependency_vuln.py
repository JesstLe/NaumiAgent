# ruff: noqa: E501
"""Dependency vulnerability scan module (local DB + OSV.dev)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

NPM_PACKAGE_MAP: dict[str, str] = {
    "jQuery": "jquery",
    "jQuery Migrate": "jquery-migrate",
    "AngularJS": "angular",
    "React": "react",
    "Vue.js": "vue",
    "Bootstrap": "bootstrap",
    "Lodash": "lodash",
    "Moment.js": "moment",
    "Axios": "axios",
    "D3.js": "d3",
    "Underscore.js": "underscore",
    "Dojo": "dojo",
    "Prototype.js": "prototype",
    "MooTools": "mootools",
}

KNOWN_VULNS: dict[str, list[dict[str, str]]] = {
    "jQuery": [
        {
            "before": "3.5.0",
            "cve": "CVE-2020-11022/11023",
            "desc": "XSS via .html() and DOM manipulation",
            "severity": "high",
        },
        {
            "before": "3.4.1",
            "cve": "CVE-2019-11358",
            "desc": "Prototype pollution via extend",
            "severity": "medium",
        },
        {
            "before": "3.0.0",
            "cve": "CVE-2015-9251",
            "desc": "XSS via cross-domain ajax",
            "severity": "high",
        },
        {
            "before": "2.2.0",
            "cve": "CVE-2012-6708",
            "desc": "Selector DoS",
            "severity": "medium",
        },
    ],
    "AngularJS": [
        {
            "before": "1.8.0",
            "cve": "CVE-2020-7676",
            "desc": "Regex DoS in angular.js",
            "severity": "medium",
        },
        {
            "before": "1.7.9",
            "cve": "CVE-2019-10768",
            "desc": "Prototype pollution",
            "severity": "high",
        },
        {
            "before": "1.6.9",
            "cve": "CVE-2018-16758",
            "desc": "XSS",
            "severity": "medium",
        },
        {
            "before": "1.6.0",
            "cve": "CVE-2019-14864",
            "desc": "Arbitrary code execution",
            "severity": "high",
        },
    ],
    "Lodash": [
        {
            "before": "4.17.21",
            "cve": "CVE-2021-23337",
            "desc": "Command injection via template",
            "severity": "high",
        },
        {
            "before": "4.17.20",
            "cve": "CVE-2020-8203",
            "desc": "Prototype pollution via zipObjectDeep",
            "severity": "high",
        },
        {
            "before": "4.17.16",
            "cve": "CVE-2020-8192",
            "desc": "Prototype pollution via setWith",
            "severity": "medium",
        },
    ],
    "Bootstrap": [
        {
            "before": "4.3.1",
            "cve": "CVE-2019-8331",
            "desc": "XSS in tooltip",
            "severity": "medium",
        },
        {
            "before": "3.4.1",
            "cve": "CVE-2019-8331",
            "desc": "XSS in tooltip (v3)",
            "severity": "medium",
        },
    ],
    "Moment.js": [
        {
            "before": "2.29.4",
            "cve": "CVE-2022-31129",
            "desc": "ReDoS via locale callback",
            "severity": "medium",
        },
        {
            "before": "2.29.2",
            "cve": "CVE-2022-24785",
            "desc": "ReDoS via parse()",
            "severity": "medium",
        },
    ],
    "React": [
        {
            "before": "18.0.0",
            "cve": "N/A",
            "desc": "React < 18 — check for known CVEs",
            "severity": "low",
        },
    ],
    "Vue.js": [
        {
            "before": "3.0.0",
            "cve": "CVE-2022-23646",
            "desc": "Prototype pollution in Vue 2",
            "severity": "high",
        },
        {
            "before": "2.6.13",
            "cve": "CVE-2021-31865",
            "desc": "Arbitrary code execution via slot",
            "severity": "high",
        },
    ],
    "Axios": [
        {
            "before": "0.21.2",
            "cve": "CVE-2021-3749",
            "desc": "ReDoS via typeof check",
            "severity": "high",
        },
        {
            "before": "0.21.1",
            "cve": "CVE-2020-28168",
            "desc": "SSRF via proxy headers",
            "severity": "medium",
        },
    ],
    "D3.js": [
        {
            "before": "7.0.0",
            "cve": "N/A",
            "desc": "D3 < 7 — check for known issues",
            "severity": "low",
        },
    ],
    "Dojo": [
        {
            "before": "1.16.0",
            "cve": "CVE-2020-5258",
            "desc": "Prototype pollution",
            "severity": "high",
        },
        {
            "before": "1.14.0",
            "cve": "CVE-2019-10773",
            "desc": "Prototype pollution via deepCopy",
            "severity": "high",
        },
    ],
    "Prototype.js": [
        {
            "before": "1.7.3",
            "cve": "CVE-2015-6714",
            "desc": "Prototype pollution via evalJSON",
            "severity": "high",
        },
    ],
    "MooTools": [
        {
            "before": "1.6.0",
            "cve": "CVE-2020-7773",
            "desc": "Prototype pollution",
            "severity": "high",
        },
    ],
    "jQuery Migrate": [
        {
            "before": "3.3.0",
            "cve": "N/A",
            "desc": "May expose deprecated APIs",
            "severity": "low",
        },
    ],
    "Underscore.js": [
        {
            "before": "1.13.0",
            "cve": "CVE-2021-23358",
            "desc": "Prototype pollution via template",
            "severity": "high",
        },
    ],
}


def _parse_version(v: str) -> tuple[int, int, int]:
    parts = str(v).split(".")
    return (
        int(parts[0]) if len(parts) > 0 else 0,
        int(parts[1]) if len(parts) > 1 else 0,
        int(parts[2]) if len(parts) > 2 else 0,
    )


def _is_before(version: str, threshold: str) -> bool:
    return _parse_version(version) < _parse_version(threshold)


def _osv_severity_to_standard(severity: Any) -> str:
    if not severity:
        return "medium"
    cvss = severity.get("cvss", [{}]) if isinstance(severity, dict) else []
    if cvss:
        score = cvss[0].get("scoreV3", cvss[0].get("scoreV2"))
        if isinstance(score, (int, float)):
            if score >= 9.0:
                return "critical"
            if score >= 7.0:
                return "high"
            if score >= 4.0:
                return "medium"
            return "low"
    return "medium"


async def _query_osv_dev(page: Any, packages: list[dict[str, str]]) -> list[Any]:
    """Query OSV.dev batch API for dependency vulnerabilities."""
    if not packages:
        return []
    queries = [
        {"package": {"name": p["name"], "ecosystem": "npm"}, "version": p["version"]}
        for p in packages
    ]
    try:
        result = await page.evaluate(
            """async (queries) => {
                try {
                    const res = await fetch("https://api.osv.dev/v1/querybatch", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ queries }),
                        signal: AbortSignal.timeout(15000),
                    });
                    if (!res.ok) return [];
                    const data = await res.json();
                    return data.results || [];
                } catch { return []; }
            }""",
            queries,
        )
        return result if isinstance(result, list) else []
    except Exception:
        return []


async def _fetch_osv_vuln_details(page: Any, vuln_id: str) -> dict[str, Any] | None:
    """Fetch full vulnerability details from OSV.dev."""
    try:
        result = await page.evaluate(
            """async (id) => {
                try {
                    const res = await fetch("https://api.osv.dev/v1/vulns/" + id, {
                        signal: AbortSignal.timeout(8000),
                    });
                    if (!res.ok) return null;
                    return await res.json();
                } catch { return null; }
            }""",
            vuln_id,
        )
        return result if isinstance(result, dict) else None
    except Exception:
        return None


async def scan_dependency_vulns(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Detect frontend libraries and check for known vulnerabilities (local + OSV.dev)."""
    url = page.url
    findings: list[dict[str, Any]] = []

    libraries = await page.evaluate(
        """() => {
            const detected = [];
            const globals = {
                jQuery: { test: () => typeof jQuery !== "undefined" && jQuery.fn?.jquery, name: "jQuery" },
                jQuery_migrate: { test: () => typeof jQuery !== "undefined" && jQuery.migrateVersion, name: "jQuery Migrate" },
                angular: { test: () => typeof angular !== "undefined" && angular.version?.full, name: "AngularJS" },
                react: { test: () => typeof React !== "undefined" && React.version, name: "React" },
                vue: { test: () => typeof Vue !== "undefined" && Vue.version, name: "Vue.js" },
                bootstrap: { test: () => typeof bootstrap !== "undefined" && bootstrap.Tooltip?.VERSION, name: "Bootstrap" },
                lodash: { test: () => typeof _ !== "undefined" && _.VERSION, name: "Lodash" },
                moment: { test: () => typeof moment !== "undefined" && moment.version, name: "Moment.js" },
                axios: { test: () => typeof axios !== "undefined" && axios.VERSION, name: "Axios" },
                d3: { test: () => typeof d3 !== "undefined" && d3.version, name: "D3.js" },
                underscore: { test: () => typeof _ !== "undefined" && _.VERSION && typeof jQuery === "undefined", name: "Underscore.js" },
                dojo: { test: () => typeof dojo !== "undefined" && dojo.version, name: "Dojo" },
                prototype: { test: () => typeof Prototype !== "undefined" && Prototype.Version, name: "Prototype.js" },
                mooTools: { test: () => typeof MooTools !== "undefined" && MooTools.version, name: "MooTools" },
            };
            for (const [key, lib] of Object.entries(globals)) {
                try {
                    const version = lib.test();
                    if (version) detected.push({ name: lib.name, version: String(version), method: "global" });
                } catch {}
            }

            const scripts = Array.from(document.querySelectorAll("script[src]"));
            const libPatterns = [
                { pattern: /jquery[.\\-](\\d+\\.\\d+\\.\\d+)/i, name: "jQuery" },
                { pattern: /angular(?:\\.min)?\\.js/i, name: "AngularJS" },
                { pattern: /vue(?:\\.global)?\\.js/i, name: "Vue.js" },
                { pattern: /bootstrap[.\\-](\\d+\\.\\d+\\.\\d+)/i, name: "Bootstrap" },
                { pattern: /lodash(?:\\.min)?\\.js/i, name: "Lodash" },
                { pattern: /moment(?:\\.min)?\\.js/i, name: "Moment.js" },
            ];
            for (const script of scripts) {
                for (const { pattern, name } of libPatterns) {
                    if (pattern.test(script.src) && !detected.some((d) => d.name === name)) {
                        const verMatch = script.src.match(/(\\d+\\.\\d+\\.\\d+)/);
                        detected.push({ name, version: verMatch ? verMatch[1] : "unknown", method: "script-src" });
                    }
                }
            }
            return detected;
        }"""
    )

    # Local vulnerability check
    for lib in libraries:
        version = lib.get("version", "unknown")
        if version == "unknown":
            findings.append(
                add_finding(
                    {
                        "category": "dependency-vuln",
                        "severity": "info",
                        "title": f"{lib['name']} detected (version unknown)",
                        "description": f"{lib['name']} found but version could not be determined",
                        "url": url,
                        "evidence": {"name": lib["name"], "version": "unknown", "method": lib.get("method")},
                    }
                )
            )
            continue

        vulns = KNOWN_VULNS.get(lib["name"], [])
        matched = False
        for vuln in vulns:
            if _is_before(version, vuln["before"]):
                matched = True
                findings.append(
                    add_finding(
                        {
                            "category": "dependency-vuln",
                            "severity": vuln["severity"],
                            "title": f"{lib['name']} {version} vulnerable ({vuln['cve']})",
                            "description": vuln["desc"],
                            "url": url,
                            "evidence": {
                                "name": lib["name"],
                                "version": version,
                                "cve": vuln["cve"],
                                "fixedIn": vuln["before"],
                            },
                        }
                    )
                )

        if not matched:
            findings.append(
                add_finding(
                    {
                        "category": "dependency-vuln",
                        "severity": "info",
                        "title": f"{lib['name']} {version} detected",
                        "description": f"{lib['name']} version {version} — no known vulnerabilities",
                        "url": url,
                        "evidence": {
                            "name": lib["name"],
                            "version": version,
                            "method": lib.get("method"),
                        },
                    }
                )
            )

    # OSV.dev online lookup
    npm_packages = [
        {
            "displayName": lib["name"],
            "name": NPM_PACKAGE_MAP.get(lib["name"], lib["name"].lower()),
            "version": lib["version"],
        }
        for lib in libraries
        if lib.get("version") != "unknown"
    ]

    osv_errors = 0
    if npm_packages:
        osv_results = await _query_osv_dev(page, npm_packages)
        existing_cve_ids = {
            f.get("evidence", {}).get("cve")
            for f in findings
            if f.get("evidence", {}).get("cve") and f["evidence"]["cve"] != "N/A"
        }
        osv_ids: set[str] = set()

        for i, result in enumerate(osv_results):
            if not result or not result.get("vulns"):
                continue
            pkg = npm_packages[i]
            for vuln in result["vulns"]:
                vid = vuln.get("id", "")
                if vid in osv_ids:
                    continue
                osv_ids.add(vid)

                detail = await _fetch_osv_vuln_details(page, vid)
                if not detail:
                    osv_errors += 1
                    continue

                aliases = detail.get("aliases", [])
                if any(a in existing_cve_ids for a in aliases):
                    continue

                severity = _osv_severity_to_standard(
                    detail.get("database_specific", {}).get("severity")
                    or detail.get("severity")
                )
                summary = detail.get("summary", vid)
                cve_alias = next((a for a in aliases if a.startswith("CVE-")), "")

                findings.append(
                    add_finding(
                        {
                            "category": "dependency-vuln",
                            "severity": severity,
                            "title": f"{pkg['displayName']} {pkg['version']} — {summary} [OSV]",
                            "description": detail.get("details", summary),
                            "url": url,
                            "evidence": {
                                "name": pkg["displayName"],
                                "version": pkg["version"],
                                "osvId": vid,
                                "cve": cve_alias or vid,
                                "severity": severity,
                                "source": "osv.dev",
                                "vulnUrl": f"https://osv.dev/vulnerability/{vid}",
                            },
                        }
                    )
                )
                if cve_alias:
                    existing_cve_ids.add(cve_alias)

    return {
        "category": "dependency-vuln",
        "findings": findings,
        "librariesDetected": len(libraries),
        "osvQueried": len(npm_packages),
        "osvErrors": osv_errors,
        "source": "local+osv.dev" if npm_packages else "local",
    }
