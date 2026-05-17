# ruff: noqa: E501
"""Multi-agent security scan coordinator.

Ported from browser-debugging-daemon/scripts/security/AgentCoordinator.js.

Runs multiple SecurityAuditor instances in parallel, each with its own
BrowserRuntime and assigned audit modules (roles), then merges and
deduplicates findings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime

from .auditor import SecurityAuditor, finding_fingerprint

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent role definitions
# ---------------------------------------------------------------------------

AGENT_ROLES: dict[str, dict[str, Any]] = {
    "recon": {
        "label": "Recon Agent",
        "modules": [
            "security-headers", "cookies", "cors", "info-leak",
            "paths", "client-storage", "sri",
        ],
    },
    "attack": {
        "label": "Attack Agent",
        "modules": [
            "xss", "sqli", "command-injection", "ssti",
            "file-upload", "csrf",
        ],
    },
    "infra": {
        "label": "Infra Agent",
        "modules": [
            "tls", "ssrf", "open-redirect", "clickjacking",
            "jwt", "auth-bypass",
        ],
    },
    "deep": {
        "label": "Deep Scan Agent",
        "modules": [
            "api-fuzz", "dependency-vuln", "race-condition", "idor",
        ],
    },
    "quality": {
        "label": "Quality Agent",
        "modules": ["performance", "accessibility"],
    },
}

ALL_ROLE_NAMES = list(AGENT_ROLES.keys())


@dataclass
class AgentReport:
    """Result from a single agent scan."""

    role: str
    label: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    module_results: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class MergedReport:
    """Merged result from all agent scans."""

    target: str = ""
    started_at: str = ""
    finished_at: str = ""
    agents: list[str] = field(default_factory=list)
    agent_reports: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentCoordinator
# ---------------------------------------------------------------------------


class AgentCoordinator:
    """Orchestrate parallel multi-agent security scans.

    Parameters
    ----------
    base_dir:
        Base directory for runtime artifacts (downloads, screenshots, etc.).
    concurrency:
        Maximum number of agents running in parallel.
    headless:
        Whether to launch browsers in headless mode.
    timeout:
        Per-navigation timeout in milliseconds.
    """

    def __init__(
        self,
        base_dir: str,
        *,
        concurrency: int = 3,
        headless: bool = True,
        timeout: int = 30000,
    ) -> None:
        self.base_dir = base_dir
        self.concurrency = concurrency
        self.headless = headless
        self.timeout = timeout
        self.merged_results: list[dict[str, Any]] = []
        self.logs: list[dict[str, str]] = []

    def _log(self, role: str, msg: str) -> None:
        line = f"[{role}] {msg}"
        self.logs.append({"role": role, "message": msg})
        logger.info(line)

    async def _run_agent_scan(
        self,
        role: str,
        target_url: str,
        modules: list[str],
    ) -> AgentReport:
        """Launch a runtime, run assigned modules, return findings."""
        cfg = AGENT_ROLES.get(role, {})
        label = cfg.get("label", role)
        started_at = time.monotonic()
        runtime = BrowserRuntime(base_dir=self.base_dir)

        try:
            await runtime.start(source="managed", headless=self.headless)
            await runtime.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=self.timeout,
            )

            self._log(role, f"scanning {len(modules)} modules: {', '.join(modules)}")

            auditor = SecurityAuditor(runtime)
            all_audit_methods = {
                "security-headers": auditor.audit_security_headers,
                "cookies": auditor.audit_cookies,
                "cors": auditor.audit_cors,
                "info-leak": auditor.scan_info_leaks,
                "xss": auditor.test_xss,
                "sqli": auditor.test_sqli,
                "paths": auditor.brute_path,
                "csrf": auditor.detect_csrf,
                "command-injection": auditor.test_command_injection,
                "ssrf": auditor.test_ssrf,
                "idor": auditor.test_idor,
                "jwt": auditor.test_jwt,
                "open-redirect": auditor.test_open_redirect,
                "clickjacking": auditor.test_clickjacking,
                "file-upload": auditor.test_file_upload_bypass,
                "race-condition": auditor.test_race_condition,
                "client-storage": auditor.scan_client_storage,
                "ssti": auditor.test_ssti,
                "sri": auditor.check_subresource_integrity,
                "api-fuzz": auditor.fuzz_api,
                "tls": auditor.audit_tls,
                "auth-bypass": auditor.test_auth_bypass,
                "dependency-vuln": auditor.scan_dependency_vulns,
                "performance": auditor.analyze_performance,
                "accessibility": auditor.audit_accessibility,
            }

            module_results: dict[str, Any] = {}
            errors: list[dict[str, str]] = []

            for mod in modules:
                fn = all_audit_methods.get(mod)
                if not fn:
                    continue
                try:
                    module_results[mod] = await fn()
                except Exception as exc:
                    module_results[mod] = {"error": str(exc)}
                    errors.append({"module": mod, "error": str(exc)})

            elapsed = (time.monotonic() - started_at) * 1000
            finding_count = len(auditor.results)

            self._log(
                role,
                f"done in {elapsed / 1000:.1f}s — {finding_count} findings"
                + (f", {len(errors)} errors" if errors else ""),
            )

            return AgentReport(
                role=role,
                label=label,
                findings=list(auditor.results),
                module_results=module_results,
                elapsed_ms=elapsed,
                errors=errors,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started_at) * 1000
            self._log(role, f"failed: {exc}")
            return AgentReport(
                role=role,
                label=label,
                elapsed_ms=elapsed,
                errors=[{"module": "*", "error": str(exc)}],
            )
        finally:
            try:
                await runtime.stop()
            except Exception:
                pass

    @staticmethod
    def _roles_for_profile(profile: str) -> list[str]:
        """Select roles for a named scan profile."""
        if profile == "recon":
            return ["recon"]
        if profile == "offensive":
            return ["attack", "deep"]
        return list(ALL_ROLE_NAMES)

    async def scan(
        self,
        target_url: str,
        *,
        roles: list[str] | None = None,
        profile: str | None = None,
    ) -> MergedReport:
        """Run a multi-agent security scan against *target_url*.

        Parameters
        ----------
        target_url:
            The URL to scan.
        roles:
            Explicit list of agent roles to use.  Overrides *profile*.
        profile:
            Named profile (``"recon"``, ``"offensive"``, ``"full"``).
        """
        from datetime import datetime

        selected_roles = (
            self._roles_for_profile(profile) if profile else (roles or list(ALL_ROLE_NAMES))
        )
        started_at = datetime.now(UTC).isoformat()

        agent_reports: list[AgentReport] = []

        for i in range(0, len(selected_roles), self.concurrency):
            batch_roles = selected_roles[i : i + self.concurrency]
            tasks = [
                self._run_agent_scan(
                    role,
                    target_url,
                    AGENT_ROLES.get(role, {}).get("modules", []),
                )
                for role in batch_roles
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, Exception):
                    agent_reports.append(
                        AgentReport(
                            role="unknown",
                            label="Unknown",
                            errors=[{"module": "*", "error": str(result)}],
                        )
                    )
                else:
                    agent_reports.append(result)

        # Merge and deduplicate
        seen_fingerprints: set[str] = set()
        seen_titles: set[str] = set()
        merged_findings: list[dict[str, Any]] = []
        module_results: dict[str, Any] = {}
        all_errors: list[dict[str, str]] = []

        for report in agent_reports:
            for f in report.findings:
                fp = finding_fingerprint(f)
                if fp in seen_fingerprints:
                    continue
                seen_fingerprints.add(fp)

                loose_key = f"{f.get('category', '')}||{f.get('severity', '')}||{f.get('title', '')}"
                if f.get("severity") != "info" and loose_key in seen_titles:
                    continue
                seen_titles.add(loose_key)

                merged_findings.append({**f, "sourceAgent": report.role})

            module_results.update(report.module_results)
            all_errors.extend(report.errors)

        self.merged_results = merged_findings

        # Summary
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_agent: dict[str, int] = {}

        for f in merged_findings:
            sev = f.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            cat = f.get("category", "")
            by_category[cat] = by_category.get(cat, 0) + 1
            agent = f.get("sourceAgent", "unknown")
            by_agent[agent] = by_agent.get(agent, 0) + 1

        total_wall_ms = max(r.elapsed_ms for r in agent_reports) if agent_reports else 0
        total_cpu_ms = sum(r.elapsed_ms for r in agent_reports)

        return MergedReport(
            target=target_url,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            agents=[AGENT_ROLES.get(r, {}).get("label", r) for r in selected_roles],
            agent_reports=[
                {
                    "role": r.role,
                    "label": r.label,
                    "findings": len(r.findings),
                    "elapsed": r.elapsed_ms,
                    "errors": [e.get("module") for e in r.errors],
                }
                for r in agent_reports
            ],
            summary={
                "totalFindings": len(merged_findings),
                "bySeverity": by_severity,
                "byCategory": by_category,
                "byAgent": by_agent,
                "wallTimeMs": total_wall_ms,
                "cpuTimeMs": total_cpu_ms,
                "parallelismRatio": (
                    round(total_cpu_ms / total_wall_ms, 2) if total_wall_ms > 0 else 0
                ),
            },
            findings=merged_findings,
            errors=all_errors if all_errors else [],
        )

    async def export_merged_report(self, fmt: str = "json") -> dict[str, Any]:
        """Export merged results in the requested format."""
        if fmt == "sarif":
            categories = list({f.get("category", "") for f in self.merged_results})
            rules = [
                {"id": cat, "shortDescription": {"text": f"{cat} security check"}}
                for cat in categories
            ]
            sarif_severity_map = {
                "critical": "error",
                "high": "error",
                "medium": "warning",
                "low": "note",
                "info": "note",
            }
            results = []
            for f in self.merged_results:
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
                        "level": sarif_severity_map.get(
                            f.get("severity", "medium"), "warning"
                        ),
                        "message": {"text": f.get("description") or f.get("title", "")},
                        "locations": loc,
                        "fingerprints": {"fingerprint": finding_fingerprint(f)},
                        "properties": {
                            "severity": f.get("severity"),
                            "sourceAgent": f.get("sourceAgent"),
                            "evidence": f.get("evidence"),
                        },
                    }
                )
            return {
                "$schema": (
                    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                    "main/sarif-2.1/schema/sarif-schema-2.1.0.json"
                ),
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "name": "Multi-Agent Security Scanner",
                                "version": "1.0.0",
                                "rules": rules,
                            },
                        },
                        "results": results,
                    }
                ],
            }

        return {"findings": self.merged_results, "summary": self._build_summary()}

    def _build_summary(self) -> dict[str, Any]:
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for f in self.merged_results:
            by_severity[f.get("severity", "info")] = by_severity.get(f.get("severity", "info"), 0) + 1
            by_category[f.get("category", "")] = by_category.get(f.get("category", ""), 0) + 1
            agent = f.get("sourceAgent", "unknown")
            by_agent[agent] = by_agent.get(agent, 0) + 1
        return {
            "totalFindings": len(self.merged_results),
            "bySeverity": by_severity,
            "byCategory": by_category,
            "byAgent": by_agent,
        }

    @staticmethod
    def list_roles() -> list[dict[str, Any]]:
        """List available agent roles and their assigned modules."""
        return [
            {"name": name, "label": cfg["label"], "modules": cfg["modules"]}
            for name, cfg in AGENT_ROLES.items()
        ]
