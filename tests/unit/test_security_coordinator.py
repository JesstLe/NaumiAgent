"""Tests for AgentCoordinator — role definitions, profile selection, merge logic."""

# ruff: noqa: E501

from __future__ import annotations

import pytest

from naumi_agent.tools.browser.security.coordinator import (
    AGENT_ROLES,
    ALL_ROLE_NAMES,
    AgentCoordinator,
)


class TestAgentRoles:
    def test_all_roles_defined(self) -> None:
        assert set(ALL_ROLE_NAMES) == {"recon", "attack", "infra", "deep", "quality"}

    def test_each_role_has_label_and_modules(self) -> None:
        for name, cfg in AGENT_ROLES.items():
            assert "label" in cfg
            assert "modules" in cfg
            assert isinstance(cfg["modules"], list)
            assert len(cfg["modules"]) > 0

    def test_module_coverage(self) -> None:
        all_modules: set[str] = set()
        for cfg in AGENT_ROLES.values():
            all_modules.update(cfg["modules"])
        assert len(all_modules) >= 20

    def test_list_roles(self) -> None:
        roles = AgentCoordinator.list_roles()
        assert len(roles) == 5
        names = {r["name"] for r in roles}
        assert names == set(ALL_ROLE_NAMES)


class TestProfileSelection:
    def test_recon_profile(self) -> None:
        roles = AgentCoordinator._roles_for_profile("recon")
        assert roles == ["recon"]

    def test_offensive_profile(self) -> None:
        roles = AgentCoordinator._roles_for_profile("offensive")
        assert set(roles) == {"attack", "deep"}

    def test_full_profile(self) -> None:
        roles = AgentCoordinator._roles_for_profile("full")
        assert set(roles) == set(ALL_ROLE_NAMES)

    def test_unknown_defaults_to_all(self) -> None:
        roles = AgentCoordinator._roles_for_profile("unknown")
        assert set(roles) == set(ALL_ROLE_NAMES)


class TestCoordinatorInit:
    def test_defaults(self) -> None:
        coord = AgentCoordinator(base_dir="/tmp")
        assert coord.concurrency == 3
        assert coord.headless is True
        assert coord.timeout == 30000
        assert coord.merged_results == []
        assert coord.logs == []

    def test_custom_params(self) -> None:
        coord = AgentCoordinator(
            base_dir="/tmp",
            concurrency=5,
            headless=False,
            timeout=10000,
        )
        assert coord.concurrency == 5
        assert coord.headless is False
        assert coord.timeout == 10000


class TestMergeLogic:
    """Test merge/dedup logic using the coordinator's internal _build_summary."""

    def setup_method(self) -> None:
        self.coord = AgentCoordinator(base_dir="/tmp")

    def test_build_summary_empty(self) -> None:
        summary = self.coord._build_summary()
        assert summary["totalFindings"] == 0

    def test_build_summary_with_findings(self) -> None:
        self.coord.merged_results = [
            {"severity": "critical", "category": "xss", "sourceAgent": "attack"},
            {"severity": "high", "category": "sqli", "sourceAgent": "attack"},
            {"severity": "critical", "category": "xss", "sourceAgent": "deep"},
        ]
        summary = self.coord._build_summary()
        assert summary["totalFindings"] == 3
        assert summary["bySeverity"]["critical"] == 2
        assert summary["byAgent"]["attack"] == 2

    def test_log_recording(self) -> None:
        self.coord._log("recon", "test message")
        assert len(self.coord.logs) == 1
        assert self.coord.logs[0]["role"] == "recon"
        assert self.coord.logs[0]["message"] == "test message"
