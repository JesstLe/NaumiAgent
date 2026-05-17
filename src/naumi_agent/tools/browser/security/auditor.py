# ruff: noqa: E501
"""Security Auditor — 25-module browser security scanner.

Ported from browser-debugging-daemon/scripts/security/SecurityAuditor.js.

Each audit module runs browser-side ``page.evaluate()`` checks via the
BrowserRuntime's Playwright page object.  Results are collected as structured
findings with severity levels (info/low/medium/high/critical).

Supported capabilities:
- 25 audit modules covering headers, cookies, CORS, XSS, SQLi, CSRF,
  command injection, SSRF, IDOR, JWT, open redirect, clickjacking,
  file upload, race condition, client storage, SSTI, SRI, API fuzzing,
  TLS, auth bypass, dependency vulnerabilities (local + OSV.dev),
  performance (Core Web Vitals), and accessibility (WCAG).
- Scan profiles: quick (8 modules), standard (15 modules), full (all 25).
- Report export: JSON, SARIF (GitHub Advanced Security), HTML (dark theme).
- Baseline comparison: fingerprint-based save/load/compare.
- False positive suppression: ignore rules by category, severity, title, fingerprint.
- OSV.dev online CVE lookup for frontend dependencies.
- Concurrent batch execution with configurable batch size.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime

from .modules import (
    audit_accessibility,
    audit_cookies,
    audit_cors,
    audit_security_headers,
    audit_tls,
    check_subresource_integrity,
    detect_csrf,
    fuzz_api,
    scan_client_storage,
    scan_dependency_vulns,
    scan_info_leaks,
    test_auth_bypass,
    test_clickjacking,
    test_command_injection,
    test_file_upload_bypass,
    test_idor,
    test_jwt,
    test_open_redirect,
    test_race_condition,
    test_sqli,
    test_ssrf,
    test_ssti,
    test_xss,
)
from .modules.brute_path import brute_path
from .modules.performance import analyze_performance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_LEVELS = ("info", "low", "medium", "high", "critical")

SCAN_PROFILES: dict[str, list[str] | None] = {
    "quick": [
        "security-headers",
        "cookies",
        "cors",
        "csrf",
        "clickjacking",
        "tls",
        "sri",
        "client-storage",
    ],
    "standard": [
        "security-headers",
        "cookies",
        "cors",
        "info-leak",
        "xss",
        "sqli",
        "csrf",
        "clickjacking",
        "ssrf",
        "jwt",
        "open-redirect",
        "tls",
        "dependency-vuln",
        "sri",
        "client-storage",
    ],
    "full": None,
}

CONCURRENT_BATCH_SIZE = 4


def severity_rank(level: str) -> int:
    """Return numeric rank for a severity level (0=info, 4=critical)."""
    try:
        return SEVERITY_LEVELS.index(level)
    except ValueError:
        return 0


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Deterministic fingerprint for deduplication and baseline comparison."""
    parts: list[str] = [
        finding.get("category", ""),
        finding.get("severity", ""),
        finding.get("title", ""),
    ]
    evidence = finding.get("evidence")
    if isinstance(evidence, dict):
        cve = evidence.get("cve")
        if cve:
            parts.append(cve)
        name = evidence.get("name")
        version = evidence.get("version")
        if name and version:
            parts.append(f"{name}@{version}")
    return "||".join(parts)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreRule:
    """Rule for suppressing false-positive findings."""

    category: str | None = None
    severity: str | None = None
    title_contains: str | None = None
    fingerprint: str | None = None
    added_at: str = ""


@dataclass
class BaselineComparison:
    """Result of comparing current scan against a saved baseline."""

    baseline_size: int = 0
    new_findings: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_new(self) -> int:
        return len(self.new_findings)

    @property
    def is_resolved(self) -> int:
        return len(self.resolved)


# ---------------------------------------------------------------------------
# SecurityAuditor
# ---------------------------------------------------------------------------


class SecurityAuditor:
    """25-module browser security scanner with report export and baselines.

    Parameters
    ----------
    runtime:
        A started ``BrowserRuntime`` with an active page.
    """

    def __init__(self, runtime: BrowserRuntime) -> None:
        self.runtime = runtime
        self.results: list[dict[str, Any]] = []
        self._ignore_list: list[IgnoreRule] = []
        self._baseline_fingerprints: list[str] = []
        self._baseline_path: str | None = None

    # ── Finding management ─────────────────────────────────────────────

    def _add_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        """Record a finding with auto-assigned id and timestamp."""
        entry: dict[str, Any] = {
            "id": len(self.results) + 1,
            **finding,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.results.append(entry)
        return entry

    # ── Ignore list (false positive suppression) ───────────────────────

    def load_ignore_list(self, file_path: str) -> None:
        """Load ignore rules from a JSON file."""
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
            items = json.loads(raw)
            self._ignore_list = [
                IgnoreRule(
                    category=r.get("category"),
                    severity=r.get("severity"),
                    title_contains=r.get("titleContains"),
                    fingerprint=r.get("fingerprint"),
                    added_at=r.get("addedAt", ""),
                )
                for r in items
            ]
        except (OSError, json.JSONDecodeError):
            self._ignore_list = []

    def save_ignore_list(self, file_path: str) -> None:
        """Persist ignore rules to a JSON file."""
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "category": r.category,
                "severity": r.severity,
                "titleContains": r.title_contains,
                "fingerprint": r.fingerprint,
                "addedAt": r.added_at,
            }
            for r in self._ignore_list
        ]
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_ignore(self, rule: IgnoreRule) -> None:
        """Add a single ignore rule."""
        self._ignore_list.append(
            IgnoreRule(
                category=rule.category,
                severity=rule.severity,
                title_contains=rule.title_contains,
                fingerprint=rule.fingerprint,
                added_at=datetime.now(UTC).isoformat(),
            )
        )

    def get_ignore_list(self) -> list[IgnoreRule]:
        """Return a copy of the ignore list."""
        return list(self._ignore_list)

    def _is_ignored(self, finding: dict[str, Any]) -> bool:
        """Check if a finding matches any ignore rule."""
        for rule in self._ignore_list:
            if rule.category and rule.category != finding.get("category"):
                continue
            if rule.severity and rule.severity != finding.get("severity"):
                continue
            tc = rule.title_contains
            if tc and tc not in finding.get("title", ""):
                continue
            if rule.fingerprint and finding_fingerprint(finding) != rule.fingerprint:
                continue
            return True
        return False

    def get_filtered_results(self) -> list[dict[str, Any]]:
        """Return results with ignored findings excluded."""
        return [f for f in self.results if not self._is_ignored(f)]

    # ── Baseline comparison ────────────────────────────────────────────

    def load_baseline(self, file_path: str) -> None:
        """Load baseline fingerprints from a JSON file."""
        self._baseline_path = file_path
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "fingerprints" in data:
                self._baseline_fingerprints = data["fingerprints"]
            elif isinstance(data, list):
                self._baseline_fingerprints = [
                    f if isinstance(f, str) else finding_fingerprint(f) for f in data
                ]
            else:
                self._baseline_fingerprints = []
        except (OSError, json.JSONDecodeError):
            self._baseline_fingerprints = []

    def save_baseline(self, file_path: str | None = None) -> None:
        """Save current results' fingerprints as a new baseline."""
        dest = file_path or self._baseline_path
        if not dest:
            return
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        fingerprints = [finding_fingerprint(f) for f in self.results]
        p.write_text(
            json.dumps(
                {
                    "savedAt": datetime.now(UTC).isoformat(),
                    "fingerprints": fingerprints,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def compare_to_baseline(self) -> BaselineComparison:
        """Compare current results against the loaded baseline."""
        baseline_set = set(self._baseline_fingerprints)
        new_findings: list[dict[str, Any]] = []
        resolved: list[dict[str, str]] = []

        for f in self.results:
            if finding_fingerprint(f) not in baseline_set:
                new_findings.append(f)

        if baseline_set:
            current_fps = {finding_fingerprint(f) for f in self.results}
            for fp in baseline_set:
                if fp not in current_fps:
                    resolved.append({"fingerprint": fp, "status": "resolved"})

        return BaselineComparison(
            baseline_size=len(baseline_set),
            new_findings=new_findings,
            resolved=resolved,
        )

    # ── Scan profiles ──────────────────────────────────────────────────

    @staticmethod
    def get_profile(name: str | None) -> list[str] | None:
        """Return the module list for a named profile (``None`` = all)."""
        if not name or name == "full":
            return None
        return SCAN_PROFILES.get(name)

    @staticmethod
    def list_profiles() -> list[dict[str, Any]]:
        """List available scan profiles."""
        return [
            {
                "name": name,
                "modules": modules if modules else "all (25 modules)",
                "count": len(modules) if modules else 25,
            }
            for name, modules in SCAN_PROFILES.items()
        ]

    # ── Individual audit methods ───────────────────────────────────────
    # Each delegates to the matching function in .modules

    async def audit_security_headers(self) -> dict[str, Any]:
        return await audit_security_headers(self.runtime.page, self._add_finding)

    async def audit_cookies(self) -> dict[str, Any]:
        return await audit_cookies(self.runtime.page, self._add_finding)

    async def audit_cors(self) -> dict[str, Any]:
        return await audit_cors(self.runtime.page, self._add_finding)

    async def scan_info_leaks(self) -> dict[str, Any]:
        return await scan_info_leaks(self.runtime.page, self._add_finding)

    async def test_xss(self) -> dict[str, Any]:
        return await test_xss(self.runtime.page, self._add_finding)

    async def test_sqli(self) -> dict[str, Any]:
        return await test_sqli(self.runtime.page, self._add_finding)

    async def brute_path(self) -> dict[str, Any]:
        return await brute_path(self.runtime.page, self._add_finding)

    async def detect_csrf(self) -> dict[str, Any]:
        return await detect_csrf(self.runtime.page, self._add_finding)

    async def test_command_injection(self) -> dict[str, Any]:
        return await test_command_injection(self.runtime.page, self._add_finding)

    async def test_ssrf(self) -> dict[str, Any]:
        return await test_ssrf(self.runtime.page, self._add_finding)

    async def test_idor(self) -> dict[str, Any]:
        return await test_idor(self.runtime.page, self._add_finding)

    async def test_jwt(self) -> dict[str, Any]:
        return await test_jwt(self.runtime.page, self._add_finding)

    async def test_open_redirect(self) -> dict[str, Any]:
        return await test_open_redirect(self.runtime.page, self._add_finding)

    async def test_clickjacking(self) -> dict[str, Any]:
        return await test_clickjacking(self.runtime.page, self._add_finding)

    async def test_file_upload_bypass(self) -> dict[str, Any]:
        return await test_file_upload_bypass(self.runtime.page, self._add_finding)

    async def test_race_condition(self) -> dict[str, Any]:
        return await test_race_condition(self.runtime.page, self._add_finding)

    async def scan_client_storage(self) -> dict[str, Any]:
        return await scan_client_storage(self.runtime.page, self._add_finding)

    async def test_ssti(self) -> dict[str, Any]:
        return await test_ssti(self.runtime.page, self._add_finding)

    async def check_subresource_integrity(self) -> dict[str, Any]:
        return await check_subresource_integrity(self.runtime.page, self._add_finding)

    async def fuzz_api(self) -> dict[str, Any]:
        return await fuzz_api(self.runtime.page, self._add_finding)

    async def audit_tls(self) -> dict[str, Any]:
        return await audit_tls(self.runtime.page, self._add_finding)

    async def test_auth_bypass(self) -> dict[str, Any]:
        return await test_auth_bypass(self.runtime.page, self._add_finding)

    async def scan_dependency_vulns(self) -> dict[str, Any]:
        return await scan_dependency_vulns(self.runtime.page, self._add_finding)

    async def analyze_performance(self) -> dict[str, Any]:
        return await analyze_performance(self.runtime.page, self._add_finding)

    async def audit_accessibility(self) -> dict[str, Any]:
        return await audit_accessibility(self.runtime.page, self._add_finding)

    # ── Query helpers ──────────────────────────────────────────────────

    def get_results(
        self,
        *,
        category: str | None = None,
        min_severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter results by category and/or minimum severity."""
        filtered = list(self.results)
        if category:
            filtered = [r for r in filtered if r.get("category") == category]
        if min_severity:
            min_rank = severity_rank(min_severity)
            filtered = [
                r for r in filtered if severity_rank(r.get("severity", "info")) >= min_rank
            ]
        return filtered

    def get_summary(self) -> dict[str, Any]:
        """Aggregate summary of all findings."""
        by_category: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in self.results:
            by_category[f.get("category", "")] = by_category.get(f.get("category", ""), 0) + 1
            sev = f.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1
        return {
            "totalFindings": len(self.results),
            "byCategory": by_category,
            "bySeverity": by_severity,
            "criticalCount": by_severity.get("critical", 0),
            "highCount": by_severity.get("high", 0),
        }

    def clear(self) -> None:
        """Clear all collected findings."""
        self.results.clear()

    # ── Report export ──────────────────────────────────────────────────

    async def export_report(self, *, fmt: str = "html") -> dict[str, Any]:
        """Export findings in the requested format.

        Parameters
        ----------
        fmt:
            ``"json"``, ``"sarif"``, or ``"html"``.
        """
        summary = self.get_summary()
        all_findings = self.results

        if fmt == "json":
            return {
                "category": "report",
                "format": "json",
                "data": {"summary": summary, "findings": all_findings},
            }

        if fmt == "sarif":
            return self._export_sarif(all_findings)

        return self._export_html(summary, all_findings)

    def _export_sarif(self, all_findings: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a SARIF 2.1.0 report."""
        sarif_severity_map: dict[str, str] = {
            "critical": "error",
            "high": "error",
            "medium": "warning",
            "low": "note",
            "info": "note",
        }
        categories = list({f.get("category", "") for f in all_findings})
        rules = [
            {"id": cat, "shortDescription": {"text": f"{cat} security check"}}
            for cat in categories
        ]
        results = []
        for f in all_findings:
            loc: list[dict[str, Any]] = []
            if f.get("url"):
                loc.append(
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f["url"]},
                        },
                    }
                )
            results.append(
                {
                    "ruleId": f.get("category", ""),
                    "level": sarif_severity_map.get(f.get("severity", "medium"), "warning"),
                    "message": {"text": f.get("description") or f.get("title", "")},
                    "locations": loc,
                    "fingerprints": {"fingerprint": finding_fingerprint(f)},
                    "properties": {
                        "severity": f.get("severity"),
                        "evidence": f.get("evidence"),
                    },
                }
            )
        return {
            "category": "report",
            "format": "sarif",
            "sarif": {
                "$schema": (
                    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                    "main/sarif-2.1/schema/sarif-schema-2.1.0.json"
                ),
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "name": "Browser Security Auditor",
                                "version": "1.0.0",
                                "rules": rules,
                            },
                        },
                        "results": results,
                    }
                ],
            },
        }

    def _export_html(
        self,
        summary: dict[str, Any],
        all_findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a styled dark-theme HTML report."""
        by_severity: dict[str, list[dict[str, Any]]] = {
            "critical": [],
            "high": [],
            "medium": [],
            "low": [],
            "info": [],
        }
        for f in all_findings:
            bucket = by_severity.get(f.get("severity", "info"), by_severity["info"])
            bucket.append(f)

        generated = datetime.now(UTC).isoformat()
        now_str = generated

        category_count = len(summary.get("byCategory", {}))

        findings_html = ""
        for sev, items in by_severity.items():
            if not items:
                continue
            findings_html += f"<h2>{sev.upper()} ({len(items)})</h2>\n"
            for f in items:
                evidence_html = ""
                ev = f.get("evidence")
                if ev:
                    evidence_html = (
                        f'<div class="evidence">'
                        f"{json.dumps(ev, indent=2, default=str)}</div>"
                    )
                cat = f.get("category", "")
                furl = f.get("url", "N/A")
                fts = f.get("timestamp", "")
                ftitle = f.get("title", "")
                fdesc = f.get("description", "")
                findings_html += (
                    f"<div class='finding'>\n"
                    f"<div class='title'><span class='badge {sev}'>"
                    f"{sev}</span>{ftitle}</div>\n"
                    f"<div class='meta'>{cat} &bull; {furl} "
                    f"&bull; {fts}</div>\n"
                    f"<div class='desc'>{fdesc}</div>\n"
                    f"{evidence_html}\n</div>\n"
                )

        # fmt: off — embedded HTML/CSS template
        crit_count = summary.get("bySeverity", {}).get("critical", 0)
        high_count = summary.get("bySeverity", {}).get("high", 0)
        med_count = summary.get("bySeverity", {}).get("medium", 0)
        low_count = summary.get("bySeverity", {}).get("low", 0)
        info_count = summary.get("bySeverity", {}).get("info", 0)
        total_count = summary.get("totalFindings", 0)
        html = (
            "<!DOCTYPE html><html lang='en'><head>"
            "<meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
            "<title>Security Audit Report</title>"
            "<style>"
            "*{margin:0;padding:0;box-sizing:border-box}"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
            "background:#0d1117;color:#e6edf3;line-height:1.6;padding:2rem}"
            "h1{color:#58a6ff;border-bottom:2px solid #30363d;padding-bottom:.5rem;margin-bottom:1rem}"
            "h2{color:#79c0ff;margin:1.5rem 0 .75rem;font-size:1.2rem}"
            ".summary{display:grid;grid-template-columns:repeat(5,1fr);gap:1rem;margin:1rem 0 2rem}"
            ".stat{text-align:center;padding:1rem;border-radius:8px;border:1px solid #30363d}"
            ".stat .count{font-size:2rem;font-weight:700}"
            ".stat .label{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}"
            ".stat.critical .count{color:#f85149}.stat.critical{border-color:#f8514940}"
            ".stat.high .count{color:#d29922}.stat.high{border-color:#d2992240}"
            ".stat.medium .count{color:#58a6ff}.stat.medium{border-color:#58a6ff40}"
            ".stat.low .count{color:#3fb950}.stat.low{border-color:#3fb95040}"
            ".stat.info .count{color:#8b949e}.stat.info{border-color:#8b949e40}"
            ".finding{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1rem;margin:.5rem 0}"
            ".finding .title{font-weight:600;color:#e6edf3}"
            ".finding .meta{font-size:.85rem;color:#8b949e;margin-top:.25rem}"
            ".finding .desc{margin-top:.5rem;color:#c9d1d9}"
            ".finding .evidence{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "padding:.5rem;margin-top:.5rem;font-family:monospace;font-size:.8rem;"
            "color:#8b949e;white-space:pre-wrap;max-height:120px;overflow:auto}"
            ".badge{display:inline-block;padding:.15rem .5rem;border-radius:12px;"
            "font-size:.75rem;font-weight:600;margin-right:.5rem}"
            ".badge.critical{background:#f8514930;color:#f85149}"
            ".badge.high{background:#d2992230;color:#d29922}"
            ".badge.medium{background:#58a6ff30;color:#58a6ff}"
            ".badge.low{background:#3fb95030;color:#3fb950}"
            ".badge.info{background:#8b949e30;color:#8b949e}"
            ".timestamp{color:#8b949e;font-size:.85rem;margin-bottom:1rem}"
            "</style></head><body>"
            f"<h1>Security Audit Report</h1>"
            f"<p class='timestamp'>Generated: {now_str}</p>"
            "<div class='summary'>"
            f"<div class='stat critical'><div class='count'>{crit_count}</div>"
            "<div class='label'>Critical</div></div>"
            f"<div class='stat high'><div class='count'>{high_count}</div>"
            "<div class='label'>High</div></div>"
            f"<div class='stat medium'><div class='count'>{med_count}</div>"
            "<div class='label'>Medium</div></div>"
            f"<div class='stat low'><div class='count'>{low_count}</div>"
            "<div class='label'>Low</div></div>"
            f"<div class='stat info'><div class='count'>{info_count}</div>"
            "<div class='label'>Info</div></div></div>"
            f"<p>Total findings: <strong>{total_count}</strong> "
            f"across <strong>{category_count}</strong> categories</p>"
            f"{findings_html}"
            "</body></html>"
        )
        # fmt: on

        return {"category": "report", "format": "html", "html": html}

    # ── Full audit ─────────────────────────────────────────────────────

    async def full_audit(
        self,
        *,
        profile: str = "full",
        concurrent: bool = True,
    ) -> dict[str, Any]:
        """Run a full or profile-filtered security audit.

        Parameters
        ----------
        profile:
            Scan profile name (``"quick"``, ``"standard"``, ``"full"``).
        concurrent:
            If ``True``, run audit modules in concurrent batches.
        """
        report: dict[str, Any] = {
            "startedAt": datetime.now(UTC).isoformat(),
            "url": self.runtime.page.url if self.runtime.page else None,
            "profile": profile,
            "audits": {},
            "summary": None,
        }

        all_audits: list[tuple[str, Any]] = [
            ("security-headers", self.audit_security_headers),
            ("cookies", self.audit_cookies),
            ("cors", self.audit_cors),
            ("info-leak", self.scan_info_leaks),
            ("xss", self.test_xss),
            ("sqli", self.test_sqli),
            ("paths", self.brute_path),
            ("csrf", self.detect_csrf),
            ("command-injection", self.test_command_injection),
            ("ssrf", self.test_ssrf),
            ("idor", self.test_idor),
            ("jwt", self.test_jwt),
            ("open-redirect", self.test_open_redirect),
            ("clickjacking", self.test_clickjacking),
            ("file-upload", self.test_file_upload_bypass),
            ("race-condition", self.test_race_condition),
            ("client-storage", self.scan_client_storage),
            ("ssti", self.test_ssti),
            ("sri", self.check_subresource_integrity),
            ("api-fuzz", self.fuzz_api),
            ("tls", self.audit_tls),
            ("auth-bypass", self.test_auth_bypass),
            ("dependency-vuln", self.scan_dependency_vulns),
            ("performance", self.analyze_performance),
            ("accessibility", self.audit_accessibility),
        ]

        profile_modules = self.get_profile(profile)
        audits = (
            [(n, fn) for n, fn in all_audits if n in profile_modules]
            if profile_modules
            else all_audits
        )

        if concurrent and len(audits) > 1:
            for i in range(0, len(audits), CONCURRENT_BATCH_SIZE):
                batch = audits[i : i + CONCURRENT_BATCH_SIZE]
                coros = [fn() for _, fn in batch]
                results = await asyncio.gather(*coros, return_exceptions=True)
                for j, (name, _) in enumerate(batch):
                    r = results[j]
                    report["audits"][name] = (
                        r
                        if not isinstance(r, Exception)
                        else {"error": str(r)}
                    )
        else:
            for name, fn in audits:
                try:
                    report["audits"][name] = await fn()
                except Exception as exc:
                    report["audits"][name] = {"error": str(exc)}

        report["summary"] = self.get_summary()
        if self._baseline_fingerprints:
            report["baselineComparison"] = self.compare_to_baseline()
        report["ignoredCount"] = len(self.results) - len(self.get_filtered_results())
        report["finishedAt"] = datetime.now(UTC).isoformat()
        return report
