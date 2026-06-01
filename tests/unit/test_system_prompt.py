"""System prompt assembly tests."""

from __future__ import annotations

from naumi_agent.orchestrator.system_prompt import (
    PromptAssemblyInput,
    build_system_prompt,
    is_generated_system_prompt,
)


class TestSystemPromptAssembly:
    def test_builds_named_sections_with_marker(self) -> None:
        prompt = build_system_prompt()

        assert is_generated_system_prompt(prompt)
        assert "You are NaumiAgent" in prompt
        assert "## Your Capabilities" in prompt
        assert "## Analysis Modes" in prompt
        assert "## Task Management" in prompt
        assert "## Output Discipline" in prompt
        assert "do not paste full file contents" in prompt
        assert "## Tool Discovery" in prompt
        assert "tool_search" in prompt
        assert "## Decision Commitment" in prompt

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
