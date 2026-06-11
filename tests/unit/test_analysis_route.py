"""MoE route analysis tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.agents.message_bus import AgentMessage, AgentMessageBus
from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import MoERouteTool, _build_route_report, _scan_route
from naumi_agent.tools.analysis_tools.route import MoERouteTool as SplitMoERouteTool


def _write_route_source(path: Path) -> None:
    path.write_text(
        """
class APIService:
    def authenticate(self, jwt_token: str) -> bool:
        return bool(jwt_token)

    def query_database(self, sql: str):
        return sql
""",
        encoding="utf-8",
    )


def test_scan_route_detects_domains_and_code_shape(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    _write_route_source(source)
    text = source.read_text(encoding="utf-8")

    scan = _scan_route([source], text, "设计 auth api 和 database migration")

    assert "任务涉及领域" in scan
    assert "backend" in scan
    assert "security" in scan
    assert "代码规模" in scan


def test_build_route_report_creates_expert_panel() -> None:
    report = _build_route_report(
        "设计 auth api 和 database migration",
        "- 任务涉及领域:\n  - backend: api, database\n  - security: auth",
    )

    assert "## MoE 确定性专家路由" in report
    assert "Expert Panel" in report
    assert "backend 专家" in report
    assert "security 专家" in report
    assert "Synthesized Plan" in report


class TestMoERouteTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_panel(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "service.py"
        _write_route_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await MoERouteTool().execute(
                task="设计 auth api 和 database migration",
                target=str(source),
            )

        assert "## MoE 确定性专家路由" in output
        assert "Expert Panel" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_panel_and_adds_synthesis(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "service.py"
        _write_route_source(source)
        mock_response = ModelResponse(
            content="综合：安全专家先定义认证边界。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await MoERouteTool().execute(task="设计 auth api", target=str(source))

        assert "## MoE 确定性专家路由" in output
        assert "## LLM MoE 综合增强" in output
        assert "认证边界" in output

    @pytest.mark.asyncio
    async def test_execute_ignores_stale_subagent_manager(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "service.py"
        _write_route_source(source)
        stale_router = object()
        active_response = ModelResponse(
            content="综合：使用当前 router。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        class StaleEngine:
            _router = stale_router

        class StaleManager:
            _engine = StaleEngine()

        with (
            patch("naumi_agent.tools.analysis._global_router") as router,
            patch(
                "naumi_agent.tools.analysis._global_subagent_manager",
                StaleManager(),
            ),
        ):
            router.call = AsyncMock(return_value=active_response)
            output = await MoERouteTool().execute(task="设计 auth api", target=str(source))

        assert "## LLM MoE 综合增强" in output
        assert "## SubAgent MoE 执行结果" not in output
        assert "当前 router" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_file_router_and_manager_getter(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "service.py"
        _write_route_source(source)
        manager_calls = []

        async def run_analysis(router, system_prompt: str, user_msg: str) -> str:
            assert router == "router"
            assert "Mixture-of-Experts" in system_prompt
            assert "设计 auth api" in user_msg
            assert "任务涉及领域" in user_msg
            return "注入 MoE 综合"

        tool = SplitMoERouteTool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
            resolve_target=lambda raw: [Path(raw)],
            read_sources=lambda files: "\n".join(
                file.read_text(encoding="utf-8") for file in files
            ),
            subagent_manager_getter=lambda router: manager_calls.append(router) or None,
        )

        output = await tool.execute(task="设计 auth api", target=str(source))

        assert manager_calls == ["router"]
        assert "## MoE 确定性专家路由" in output
        assert "## LLM MoE 综合增强" in output
        assert "注入 MoE 综合" in output

    @pytest.mark.asyncio
    async def test_subagent_moe_preserves_existing_team_bus_state(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.message_bus = AgentMessageBus()

            def spawn_for_task(self, **kwargs: object) -> None:
                pass

            async def execute_parallel(self, subtasks: list[object]) -> list[AgentResult]:
                return [
                    AgentResult(
                        status="completed",
                        response="专家结论",
                        total_tokens=3,
                        total_cost_usd=0.01,
                    )
                    for _ in subtasks
                ]

            def destroy(self, name: str) -> bool:
                return True

        manager = FakeManager()
        await manager.message_bus.blackboard_set(
            "team/handoff/coder/1",
            {"content": "请接手"},
            author="coder",
        )
        await manager.message_bus.send(
            AgentMessage(
                sender="coder",
                recipient="researcher",
                topic="team.handoff",
                content="请接手",
            )
        )

        async def run_analysis(router: object, system_prompt: str, user_msg: str) -> str:
            if "EXPERT|" in system_prompt:
                return "EXPERT|Review|quality|审查实现风险"
            return "综合结论"

        tool = SplitMoERouteTool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
            subagent_manager_getter=lambda router: manager,
        )

        output = await tool._execute_with_agents(
            router="router",
            manager=manager,
            task="审查 agent 协作",
            scan_evidence="- 需要多专家",
            source_text="",
        )
        blackboard = await manager.message_bus.blackboard_get_all()
        pending = await manager.message_bus.peek("researcher")

        assert "MoE 混合专家调度报告" in output
        assert "team/handoff/coder/1" in blackboard
        assert pending[0].content == "请接手"
