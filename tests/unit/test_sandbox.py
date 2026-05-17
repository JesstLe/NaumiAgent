"""Sandbox tool tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from naumi_agent.tools.sandbox import (
    _MAX_OUTPUT_BYTES,
    CodeExecuteTool,
    _kill_process,
    _truncate,
    create_sandbox_tools,
)


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_empty_string(self):
        assert _truncate("") == ""

    def test_truncates_long_text(self):
        text = "x" * (_MAX_OUTPUT_BYTES + 1000)
        result = _truncate(text)
        assert len(result) < len(text)
        assert "截断" in result

    def test_exact_limit_not_truncated(self):
        text = "a" * _MAX_OUTPUT_BYTES
        assert _truncate(text) == text

    def test_unicode_truncation(self):
        text = "你" * (_MAX_OUTPUT_BYTES + 1000)
        result = _truncate(text)
        assert "截断" in result


class TestKillProcess:
    @pytest.mark.asyncio
    async def test_kill_calls_proc_kill(self):
        mock_proc = AsyncMock()
        await _kill_process(mock_proc)
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_ignores_process_lookup_error(self):
        mock_proc = AsyncMock()
        mock_proc.kill.side_effect = ProcessLookupError
        await _kill_process(mock_proc)


class TestCodeExecuteTool:
    def test_tool_name(self):
        assert CodeExecuteTool().name == "code_execute"

    def test_tool_description(self):
        desc = CodeExecuteTool().description
        assert "执行" in desc

    def test_tool_schema(self):
        schema = CodeExecuteTool().parameters_schema
        assert "code" in schema["properties"]
        assert "language" in schema["properties"]
        assert "timeout" in schema["properties"]
        assert schema["required"] == ["code"]

    def test_create_sandbox_tools(self):
        tools = create_sandbox_tools()
        assert len(tools) == 1
        assert tools[0].name == "code_execute"

    @pytest.mark.asyncio
    async def test_local_python_execution(self):
        import naumi_agent.tools.sandbox as sandbox_mod

        sandbox_mod._docker_available_cache = False

        tool = CodeExecuteTool()
        result = await tool.execute(code="print('hello from sandbox')")
        assert "hello from sandbox" in result

    @pytest.mark.asyncio
    async def test_local_python_error(self):
        import naumi_agent.tools.sandbox as sandbox_mod

        sandbox_mod._docker_available_cache = False

        tool = CodeExecuteTool()
        result = await tool.execute(code="raise ValueError('test error')")
        assert "exit code" in result

    @pytest.mark.asyncio
    async def test_local_timeout(self):
        import naumi_agent.tools.sandbox as sandbox_mod

        sandbox_mod._docker_available_cache = False

        tool = CodeExecuteTool()
        result = await tool.execute(
            code="import time; time.sleep(60)",
            timeout=1,
        )
        assert "timed out" in result

    @pytest.mark.asyncio
    async def test_local_no_output(self):
        import naumi_agent.tools.sandbox as sandbox_mod

        sandbox_mod._docker_available_cache = False

        tool = CodeExecuteTool()
        result = await tool.execute(code="x = 1")
        assert "no output" in result

    @pytest.mark.asyncio
    async def test_docker_check_cached(self):
        import naumi_agent.tools.sandbox as sandbox_mod

        sandbox_mod._docker_available_cache = True

        tool = CodeExecuteTool()
        # Should use cache without calling docker
        result = await tool._check_docker()
        assert result is True

        # Reset
        sandbox_mod._docker_available_cache = None
