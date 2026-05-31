"""DynamicAgentFactory tests."""

import pytest

from naumi_agent.agents.base import AgentCapability
from naumi_agent.agents.factory import DynamicAgentFactory
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine


@pytest.fixture
def engine() -> AgentEngine:
    return AgentEngine(AppConfig())


@pytest.fixture
def factory(engine: AgentEngine) -> DynamicAgentFactory:
    return DynamicAgentFactory(engine.router)


class TestCapabilityDetection:
    def test_detects_file_ops(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("read the file and edit the code")
        assert AgentCapability.FILE_OPS in caps

    def test_detects_code_exec(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("execute and build the test")
        assert AgentCapability.CODE_EXEC in caps

    def test_detects_web_search(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("search and query the database")
        assert AgentCapability.WEB_SEARCH in caps

    def test_detects_web_browse(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("browse the page and scrape url")
        assert AgentCapability.WEB_BROWSE in caps

    def test_detects_shell_exec(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("run shell bash script")
        assert AgentCapability.SHELL_EXEC in caps

    def test_no_match_returns_empty(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("hello world greeting")
        assert caps == []

    def test_chinese_keywords(self, factory: DynamicAgentFactory) -> None:
        caps = factory._detect_capabilities("读取文件并执行代码")
        assert AgentCapability.FILE_OPS in caps
        assert AgentCapability.CODE_EXEC in caps


class TestDomainDetection:
    def test_detects_backend(self, factory: DynamicAgentFactory) -> None:
        domain = factory._detect_domain("build the api server with database")
        assert domain == "backend"

    def test_detects_security(self, factory: DynamicAgentFactory) -> None:
        domain = factory._detect_domain("check jwt auth and xss vulnerability")
        assert domain == "security"

    def test_detects_ml(self, factory: DynamicAgentFactory) -> None:
        domain = factory._detect_domain("train the neural model for inference")
        assert domain == "ml"

    def test_defaults_to_general(self, factory: DynamicAgentFactory) -> None:
        domain = factory._detect_domain("something totally unrelated")
        assert domain == "通用"

    def test_picks_highest_count(self, factory: DynamicAgentFactory) -> None:
        text = "api server database sql " * 3 + "docker"
        domain = factory._detect_domain(text)
        assert domain == "backend"


class TestTierDetection:
    def test_reasoning_for_complex(self, factory: DynamicAgentFactory) -> None:
        tier = factory._detect_tier("architect the distributed system design")
        assert tier == "reasoning"

    def test_fast_for_simple(self, factory: DynamicAgentFactory) -> None:
        tier = factory._detect_tier("simple rename and minor typo fix")
        assert tier == "fast"

    def test_capable_default(self, factory: DynamicAgentFactory) -> None:
        tier = factory._detect_tier("implement the feature endpoint")
        assert tier == "capable"


class TestBudgetDetection:
    def test_fast_low_budget(self, factory: DynamicAgentFactory) -> None:
        budget = factory._detect_budget("short task", "fast")
        assert budget < 0.10

    def test_reasoning_high_budget(self, factory: DynamicAgentFactory) -> None:
        budget = factory._detect_budget("complex task", "reasoning")
        assert budget >= 0.25

    def test_long_task_more_budget(self, factory: DynamicAgentFactory) -> None:
        short = factory._detect_budget("short", "capable")
        long = factory._detect_budget("x" * 3000, "capable")
        assert long > short


class TestCreateConfig:
    def test_basic_config(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="test_agent",
            task_description="build the api server with database",
        )
        assert config.name == "test_agent"
        assert len(config.capabilities) > 0
        assert config.system_prompt != ""
        assert config.model_tier in ("fast", "capable", "reasoning")
        assert config.max_turns > 0
        assert config.max_budget_usd > 0

    def test_explicit_overrides(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="override_agent",
            task_description="build api",
            model_tier="reasoning",
            max_turns=20,
            max_budget_usd=1.0,
        )
        assert config.model_tier == "reasoning"
        assert config.max_turns == 20
        assert config.max_budget_usd == 1.0

    def test_expert_analyst_role(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="analyst",
            task_description="analyze the security architecture",
            role="expert_analyst",
            domain="security",
            focus="审查安全架构漏洞",
        )
        assert "security" in config.system_prompt.lower() or "安全" in config.system_prompt
        assert "审查安全架构漏洞" in config.system_prompt

    def test_builder_role(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="builder",
            task_description="build defensive code",
            role="builder",
            focus="编写防御性代码",
        )
        assert "编写防御性代码" in config.system_prompt

    def test_attacker_role(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="attacker",
            task_description="find vulnerabilities",
            role="attacker",
            focus="寻找漏洞",
        )
        assert "寻找漏洞" in config.system_prompt

    def test_worker_role(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="worker",
            task_description="crash analysis",
            role="worker",
            focus="识别崩溃点",
        )
        assert "识别崩溃点" in config.system_prompt

    def test_guardian_role(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="guardian",
            task_description="supervisor tree design",
            role="guardian",
            focus="隔离爆炸半径",
        )
        assert "隔离爆炸半径" in config.system_prompt

    def test_extra_capabilities(self, factory: DynamicAgentFactory) -> None:
        config = factory.create_config(
            name="extra_caps",
            task_description="hello world",
            extra_capabilities=[AgentCapability.WEB_SEARCH],
        )
        assert AgentCapability.WEB_SEARCH in config.capabilities

    def test_no_capabilities_gets_file_ops(
        self, factory: DynamicAgentFactory,
    ) -> None:
        config = factory.create_config(
            name="fallback_caps",
            task_description="analyze the design pattern",
        )
        assert AgentCapability.FILE_OPS in config.capabilities


class TestSubAgentManagerFactoryIntegration:
    def test_spawn_for_task(self, engine: AgentEngine) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager

        manager = SubAgentManager(engine)
        agent = manager.spawn_for_task(
            name="test_spawn",
            task_description="build the api server",
            role="expert_analyst",
            domain="backend",
            focus="API 设计审查",
        )
        assert agent is not None
        assert agent.config.name == "test_spawn"
        assert "backend" in agent.config.description or "API" in agent.config.description

        # Verify it's registered
        assert manager.get_agent("test_spawn") is not None

        # Verify destroy works
        assert manager.destroy("test_spawn") is True
        assert manager.get_agent("test_spawn") is None

    def test_destroy_all_dynamic(self, engine: AgentEngine) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager

        manager = SubAgentManager(engine)
        manager.spawn_for_task(
            name="dyn_1",
            task_description="task one",
        )
        manager.spawn_for_task(
            name="dyn_2",
            task_description="task two",
        )

        destroyed = manager.destroy_all_dynamic()
        assert "dyn_1" in destroyed
        assert "dyn_2" in destroyed
        assert manager.get_agent("dyn_1") is None
        assert manager.get_agent("dyn_2") is None

        # Preset agents still exist
        assert manager.get_agent("coder") is not None

    def test_cannot_destroy_preset(self, engine: AgentEngine) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager

        manager = SubAgentManager(engine)
        manager.get_agent("coder")
        assert manager.destroy("coder") is False
        assert manager.get_agent("coder") is not None
