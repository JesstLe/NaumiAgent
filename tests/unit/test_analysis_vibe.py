"""Vibe mode tool tests."""

from __future__ import annotations

import py_compile
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.analysis import VibeModeTool
from naumi_agent.tools.analysis import _build_vibe_scaffold as analysis_build_vibe_scaffold
from naumi_agent.tools.analysis_support.vibe import build_vibe_scaffold, scan_vibe_request
from naumi_agent.tools.base import ToolCall


class TestVibeScaffold:
    def test_builds_python_stdlib_scaffold_by_default(self) -> None:
        scaffold = build_vibe_scaffold("做一个任务看板")

        assert scaffold.kind == "python-stdlib-web"
        assert scaffold.run_command == "python app.py"
        assert any(name == "app.py" for name, _ in scaffold.files)
        assert any("做一个任务看板" in content for _, content in scaffold.files)
        assert analysis_build_vibe_scaffold("做一个任务看板") == scaffold

    def test_builds_node_scaffold_when_requested(self) -> None:
        scaffold = build_vibe_scaffold("计数器", "node")

        assert scaffold.kind == "node-stdlib-web"
        assert scaffold.run_command == "npm start"
        assert any(name == "server.js" for name, _ in scaffold.files)

    def test_builds_static_scaffold_when_requested(self) -> None:
        scaffold = build_vibe_scaffold("静态页", "html")

        assert scaffold.kind == "static-html"
        assert scaffold.run_command == "python -m http.server 8000"
        assert any(name == "index.html" for name, _ in scaffold.files)

    def test_scan_reports_real_generated_files(self) -> None:
        scaffold = build_vibe_scaffold("Demo", "html")
        scan = scan_vibe_request("Demo", "html", scaffold)

        assert "确定性 scaffold 类型：static-html" in scan
        assert "index.html" in scan
        assert "运行命令" in scan


class TestVibeModeTool:
    def test_metadata_declares_output_dir_path_arg(self) -> None:
        tool = VibeModeTool()

        assert tool.metadata.destructive
        assert tool.metadata.path_argument_names == ("output_dir",)

    @pytest.mark.asyncio
    async def test_execute_without_router_returns_runnable_scaffold(self) -> None:
        tool = VibeModeTool()

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await tool.execute(description="做一个最小 CRM", tech_stack="html")

        assert "## Vibe Scaffold" in output
        assert "index.html" in output
        assert "python -m http.server 8000" in output
        assert "模型路由未初始化" in output

    @pytest.mark.asyncio
    async def test_execute_writes_scaffold_to_output_dir(self, tmp_path) -> None:
        tool = VibeModeTool()

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await tool.execute(
                description="本地表单 Demo",
                tech_stack="python",
                output_dir=str(tmp_path),
            )

        app = tmp_path / "app.py"
        readme = tmp_path / "README.md"
        assert app.is_file()
        assert readme.is_file()
        assert "本地表单 Demo" in app.read_text(encoding="utf-8")
        assert str(app) in output
        py_compile.compile(str(app), doraise=True)

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_scaffold_and_adds_enhancement(self) -> None:
        tool = VibeModeTool()
        mock_response = ModelResponse(
            content="增强建议：添加输入校验。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await tool.execute(description="投票 Demo", tech_stack="python")

        assert "## Vibe Scaffold" in output
        assert "app.py" in output
        assert "## LLM 增强建议" in output
        assert "增强建议" in output

    @pytest.mark.asyncio
    async def test_engine_blocks_output_dir_outside_sandbox(self, tmp_path) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            result = await engine._execute_tool(
                ToolCall(
                    id="vibe-1",
                    name="analysis_vibe",
                    arguments=(
                        '{"description": "越界写入", '
                        '"tech_stack": "python", '
                        '"output_dir": "/etc/naumi-vibe"}'
                    ),
                )
            )
        finally:
            await engine.shutdown()

        assert result.status == "error"
        assert "权限拒绝" in result.content
        assert "不在允许目录内" in result.content
