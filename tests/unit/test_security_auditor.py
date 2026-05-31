"""Tests for SecurityAuditor core — finding management, ignore rules, baseline, profiles, export."""

# ruff: noqa: E501

from __future__ import annotations

import os
import tempfile

import pytest

from naumi_agent.tools.browser.security.auditor import (
    SEVERITY_LEVELS,
    IgnoreRule,
    SecurityAuditor,
    finding_fingerprint,
    severity_rank,
)


class FakeRuntime:
    """Minimal runtime stub for testing SecurityAuditor without a browser."""

    def __init__(self) -> None:
        self.page = None


class TestSeverityRank:
    def test_info(self) -> None:
        assert severity_rank("info") == 0

    def test_critical(self) -> None:
        assert severity_rank("critical") == 4

    def test_unknown(self) -> None:
        assert severity_rank("unknown") == 0

    def test_all_levels(self) -> None:
        for i, level in enumerate(SEVERITY_LEVELS):
            assert severity_rank(level) == i


class TestFindingFingerprint:
    def test_basic(self) -> None:
        f = {"category": "xss", "severity": "high", "title": "XSS found"}
        assert finding_fingerprint(f) == "xss||high||XSS found"

    def test_with_cve(self) -> None:
        f = {
            "category": "dep",
            "severity": "high",
            "title": "vuln",
            "evidence": {"cve": "CVE-2020-1234"},
        }
        assert "CVE-2020-1234" in finding_fingerprint(f)

    def test_with_name_version(self) -> None:
        f = {
            "category": "dep",
            "severity": "high",
            "title": "vuln",
            "evidence": {"name": "jquery", "version": "3.5.0"},
        }
        assert "jquery@3.5.0" in finding_fingerprint(f)

    def test_empty(self) -> None:
        assert finding_fingerprint({}) == "||||"


class TestSecurityAuditorFindings:
    def setup_method(self) -> None:
        self.auditor = SecurityAuditor(FakeRuntime())

    def test_add_finding(self) -> None:
        entry = self.auditor._add_finding(
            {"category": "xss", "severity": "high", "title": "test"}
        )
        assert entry["id"] == 1
        assert "timestamp" in entry
        assert len(self.auditor.results) == 1

    def test_add_multiple_findings(self) -> None:
        self.auditor._add_finding({"category": "a", "severity": "low", "title": "t1"})
        self.auditor._add_finding({"category": "b", "severity": "high", "title": "t2"})
        assert len(self.auditor.results) == 2
        assert self.auditor.results[0]["id"] == 1
        assert self.auditor.results[1]["id"] == 2

    def test_get_results_filter_category(self) -> None:
        self.auditor._add_finding({"category": "xss", "severity": "high", "title": "t1"})
        self.auditor._add_finding({"category": "sqli", "severity": "high", "title": "t2"})
        results = self.auditor.get_results(category="xss")
        assert len(results) == 1
        assert results[0]["category"] == "xss"

    def test_get_results_filter_severity(self) -> None:
        self.auditor._add_finding({"category": "a", "severity": "low", "title": "t1"})
        self.auditor._add_finding({"category": "b", "severity": "critical", "title": "t2"})
        results = self.auditor.get_results(min_severity="high")
        assert len(results) == 1
        assert results[0]["severity"] == "critical"

    def test_get_summary(self) -> None:
        self.auditor._add_finding({"category": "xss", "severity": "high", "title": "t1"})
        self.auditor._add_finding({"category": "xss", "severity": "critical", "title": "t2"})
        summary = self.auditor.get_summary()
        assert summary["totalFindings"] == 2
        assert summary["criticalCount"] == 1
        assert summary["highCount"] == 1
        assert summary["byCategory"]["xss"] == 2

    def test_clear(self) -> None:
        self.auditor._add_finding({"category": "a", "severity": "low", "title": "t"})
        self.auditor.clear()
        assert len(self.auditor.results) == 0


class TestIgnoreRules:
    def setup_method(self) -> None:
        self.auditor = SecurityAuditor(FakeRuntime())

    def test_no_rules(self) -> None:
        f = {"category": "xss", "severity": "high", "title": "test"}
        assert not self.auditor._is_ignored(f)

    def test_category_match(self) -> None:
        self.auditor.add_ignore(IgnoreRule(category="xss"))
        f = {"category": "xss", "severity": "high", "title": "test"}
        assert self.auditor._is_ignored(f)

    def test_category_no_match(self) -> None:
        self.auditor.add_ignore(IgnoreRule(category="sqli"))
        f = {"category": "xss", "severity": "high", "title": "test"}
        assert not self.auditor._is_ignored(f)

    def test_severity_match(self) -> None:
        self.auditor.add_ignore(IgnoreRule(severity="low"))
        f = {"category": "xss", "severity": "low", "title": "test"}
        assert self.auditor._is_ignored(f)

    def test_title_contains_match(self) -> None:
        self.auditor.add_ignore(IgnoreRule(title_contains="XSS"))
        f = {"category": "xss", "severity": "high", "title": "XSS found"}
        assert self.auditor._is_ignored(f)

    def test_title_contains_no_match(self) -> None:
        self.auditor.add_ignore(IgnoreRule(title_contains="SQLI"))
        f = {"category": "xss", "severity": "high", "title": "XSS found"}
        assert not self.auditor._is_ignored(f)

    def test_fingerprint_match(self) -> None:
        self.auditor._add_finding({"category": "xss", "severity": "high", "title": "test"})
        fp = finding_fingerprint(self.auditor.results[0])
        self.auditor.add_ignore(IgnoreRule(fingerprint=fp))
        assert self.auditor._is_ignored(self.auditor.results[0])

    def test_filtered_results(self) -> None:
        self.auditor._add_finding({"category": "xss", "severity": "low", "title": "t1"})
        self.auditor._add_finding({"category": "sqli", "severity": "high", "title": "t2"})
        self.auditor.add_ignore(IgnoreRule(severity="low"))
        filtered = self.auditor.get_filtered_results()
        assert len(filtered) == 1
        assert filtered[0]["category"] == "sqli"

    def test_save_load_ignore_list(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name

        try:
            self.auditor.add_ignore(IgnoreRule(category="xss", severity="high"))
            self.auditor.save_ignore_list(path)

            auditor2 = SecurityAuditor(FakeRuntime())
            auditor2.load_ignore_list(path)
            rules = auditor2.get_ignore_list()
            assert len(rules) == 1
            assert rules[0].category == "xss"
        finally:
            os.unlink(path)

    def test_load_nonexistent(self) -> None:
        self.auditor.load_ignore_list("/nonexistent/path.json")
        assert len(self.auditor.get_ignore_list()) == 0


class TestBaseline:
    def setup_method(self) -> None:
        self.auditor = SecurityAuditor(FakeRuntime())

    def test_save_load_baseline(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name

        try:
            self.auditor._add_finding({"category": "xss", "severity": "high", "title": "test"})
            self.auditor.save_baseline(path)

            auditor2 = SecurityAuditor(FakeRuntime())
            auditor2.load_baseline(path)
            assert len(auditor2._baseline_fingerprints) == 1
        finally:
            os.unlink(path)

    def test_compare_new_findings(self) -> None:
        self.auditor._add_finding({"category": "xss", "severity": "high", "title": "known"})
        self.auditor._baseline_fingerprints = [
            finding_fingerprint(self.auditor.results[0])
        ]

        self.auditor._add_finding({"category": "sqli", "severity": "high", "title": "new"})
        comp = self.auditor.compare_to_baseline()
        assert comp.is_new == 1
        assert comp.baseline_size == 1

    def test_compare_resolved(self) -> None:
        self.auditor._baseline_fingerprints = ["old||high||old finding"]
        comp = self.auditor.compare_to_baseline()
        assert comp.is_resolved == 1

    def test_load_invalid_json(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("not json")
            path = f.name
        try:
            self.auditor.load_baseline(path)
            assert self.auditor._baseline_fingerprints == []
        finally:
            os.unlink(path)


class TestProfiles:
    def test_quick_profile(self) -> None:
        modules = SecurityAuditor.get_profile("quick")
        assert modules is not None
        assert len(modules) == 8
        assert "security-headers" in modules

    def test_standard_profile(self) -> None:
        modules = SecurityAuditor.get_profile("standard")
        assert modules is not None
        assert len(modules) == 15

    def test_full_profile(self) -> None:
        modules = SecurityAuditor.get_profile("full")
        assert modules is None

    def test_unknown_profile(self) -> None:
        modules = SecurityAuditor.get_profile("unknown")
        assert modules is None

    def test_list_profiles(self) -> None:
        profiles = SecurityAuditor.list_profiles()
        assert len(profiles) == 3
        names = {p["name"] for p in profiles}
        assert names == {"quick", "standard", "full"}


class TestReportExport:
    def setup_method(self) -> None:
        self.auditor = SecurityAuditor(FakeRuntime())
        self.auditor._add_finding(
            {
                "category": "xss",
                "severity": "critical",
                "title": "XSS test",
                "description": "Found XSS",
                "url": "https://example.com",
                "evidence": {"payload": "<script>"},
            }
        )

    @pytest.mark.asyncio
    async def test_json_export(self) -> None:
        result = await self.auditor.export_report(fmt="json")
        assert result["category"] == "report"
        assert result["format"] == "json"
        assert result["data"]["summary"]["totalFindings"] == 1

    @pytest.mark.asyncio
    async def test_sarif_export(self) -> None:
        result = await self.auditor.export_report(fmt="sarif")
        assert result["format"] == "sarif"
        sarif = result["sarif"]
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"]) == 1
        assert len(sarif["runs"][0]["results"]) == 1
        assert sarif["runs"][0]["results"][0]["level"] == "error"

    @pytest.mark.asyncio
    async def test_html_export(self) -> None:
        result = await self.auditor.export_report(fmt="html")
        assert result["format"] == "html"
        assert "<!DOCTYPE html>" in result["html"]
        assert "XSS test" in result["html"]
        assert "critical" in result["html"]
