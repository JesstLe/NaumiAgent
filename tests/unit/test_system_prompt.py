"""System prompt assembly tests."""

from __future__ import annotations

import re

import pytest

from naumi_agent.orchestrator.system_prompt import (
    PromptAssemblyInput,
    build_system_prompt,
    is_generated_system_prompt,
)


class TestSystemPromptAssembly:
    def test_builds_named_sections_with_marker(self) -> None:
        prompt = build_system_prompt()

        assert is_generated_system_prompt(prompt)
        assert '<naumi_system_prompt version="sections-v2">' in prompt
        assert "You are NaumiAgent" in prompt
        assert "## Your Capabilities" in prompt
        assert "## Knowledge Freshness" in prompt
        assert "time-sensitive" in prompt
        assert "current evidence" in prompt
        assert "potentially stale" in prompt
        assert "## Analysis Tools" in prompt
        assert "## Operating Principles" in prompt
        assert "## Task Management" in prompt
        assert "## Context Hygiene" in prompt
        assert "## Output Discipline" in prompt
        assert "do not paste full file contents" in prompt
        assert "## Tool Discovery" in prompt
        assert "tool_search" in prompt
        assert "## File Discovery Discipline" in prompt
        assert "glob/grep" in prompt
        assert "read after identifying" in prompt
        assert "match count" in prompt
        assert "## UI Protocol Contract" in prompt
        assert "## Decision Discipline" in prompt
        assert "## Completion Discipline" in prompt
        assert "中文优先" in prompt
        assert "raw screenshots" in prompt
        assert "JSONL bridge events" in prompt

    @pytest.mark.parametrize(
        "marker",
        [
            '<naumi_system_prompt version="sections-v1">',
            '<naumi_system_prompt version="sections-v2">',
            '<naumi_system_prompt version="sections-v12">',
        ],
    )
    def test_generated_marker_recognizes_supported_versions(
        self,
        marker: str,
    ) -> None:
        assert is_generated_system_prompt(marker)

    @pytest.mark.parametrize(
        "content",
        [
            '<naumi_system_prompt version="custom">',
            '<naumi_system_prompt version="sections-v0">',
            '<naumi_system_prompt version="sections-v1-beta">',
            "prefix <naumi_system_prompt",
            'custom text\n<naumi_system_prompt version="sections-v1">',
            "custom prompt",
        ],
    )
    def test_generated_marker_rejects_invalid_lookalikes(
        self,
        content: str,
    ) -> None:
        assert not is_generated_system_prompt(content)

    def test_base_prompt_contains_no_snapshot_date_or_build_timestamp(self) -> None:
        prompt = build_system_prompt()

        assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", prompt) is None
        assert "Generated at" not in prompt
        assert "Build date" not in prompt

    def test_default_prompt_does_not_inline_full_analysis_catalog(self) -> None:
        prompt = build_system_prompt()

        assert "analysis_watchdog" not in prompt
        assert "analysis_chaos" not in prompt
        assert "Use analysis tools only when" in prompt
        assert len(prompt) < 7000

    def test_embeds_runtime_defaults_when_available(self) -> None:
        prompt = build_system_prompt(
            PromptAssemblyInput(
                workspace_root="/workspace/project",
                permission_mode="moderate",
                tool_names=("file_read", "file_write", "browser_goto", "mcp__demo__echo"),
                skill_names=("review", "deploy"),
            )
        )

        assert "## Runtime Defaults" in prompt
        assert "Workspace root: /workspace/project" in prompt
        assert "Permission mode: moderate" in prompt
        assert "Registered tools: 4" in prompt
        assert "file:2" in prompt
        assert "browser:1" in prompt
        assert "mcp:1" in prompt
        assert "Loaded skills: deploy, review" in prompt
