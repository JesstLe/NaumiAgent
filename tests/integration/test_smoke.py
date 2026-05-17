"""Smoke test — 验证完整系统初始化和工具注册."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine


class TestSmokeInit:
    """验证 AgentEngine 完整初始化."""

    @pytest.fixture
    def engine(self) -> AgentEngine:
        return AgentEngine(AppConfig())

    def test_all_tool_categories_registered(self, engine: AgentEngine) -> None:
        names = set(engine.tool_registry.names)
        # 内置工具
        assert "file_read" in names
        assert "file_write" in names
        assert "file_edit" in names
        assert "bash_run" in names
        # 浏览器工具
        assert "browser_goto" in names
        assert "browser_observe" in names
        # 沙箱
        assert "code_execute" in names
        # 网络
        assert "web_search" in names
        assert "web_fetch" in names
        # 记忆
        assert "memory_store" in names
        assert "memory_recall" in names
        # 子 Agent
        assert "delegate_task" in names
        assert "list_agents" in names

    def test_engine_components(self, engine: AgentEngine) -> None:
        assert engine.router is not None
        assert engine.session_store is not None
        assert engine.long_term_memory is not None
        assert engine.subagent_manager is not None
        assert engine.emitter is not None

    def test_subagent_manager_has_agents(self, engine: AgentEngine) -> None:
        agents = engine.subagent_manager.list_agents()
        names = [a["name"] for a in agents]
        assert "coder" in names
        assert "researcher" in names
        assert "browser" in names

    def test_model_info_resolution(self, engine: AgentEngine) -> None:
        model = engine.router.resolve_model("capable")
        window = engine.router.get_context_window(model)
        assert window > 0

    def test_permission_checker_active(self, engine: AgentEngine) -> None:
        from naumi_agent.safety.permissions import PermissionDecision

        decision = engine._permission_checker.check("bash_run", {"command": "ls"})
        assert isinstance(decision, PermissionDecision)

    @pytest.mark.asyncio
    async def test_session_create_and_save(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session(title="test session")
        assert session.id
        assert session.title == "test session"
        await engine.session_store.close()

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        await engine.shutdown()
        assert engine.session_store._db is None


class TestSmokeConfig:
    """验证配置加载."""

    def test_default_config(self) -> None:
        config = AppConfig()
        assert config.models.default_model == "claude-sonnet-4-6"
        assert config.safety.max_budget_usd == 5.0
        assert config.safety.max_turns == 30

    def test_yaml_config(self) -> None:
        import os

        os.environ["NAUMI_MODELS__API_KEY"] = "test-key"
        try:
            config = AppConfig.from_yaml("config.yaml")
            assert config.models.default_model == "openai/kimi-for-coding"
            assert config.models.api_key == "test-key"
        finally:
            del os.environ["NAUMI_MODELS__API_KEY"]

    def test_model_info_override(self) -> None:
        config = AppConfig.from_yaml("config.yaml")
        meta = config.models.model_info.get("openai/kimi-for-coding")
        assert meta is not None
        assert meta.max_context == 256000
