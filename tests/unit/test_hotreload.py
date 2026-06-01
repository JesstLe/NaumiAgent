"""Hot-reload tests."""


import pytest

from naumi_agent.tools.hotreload import (
    HotReloadTool,
    _is_protected,
    _normalize_reload_target,
    _resolve_modules,
    list_reloadable,
    reload_domain,
    reload_module,
)


class TestIsProtected:
    def test_protects_engine(self):
        assert _is_protected("naumi_agent.orchestrator.engine")

    def test_protects_safety(self):
        assert _is_protected("naumi_agent.safety.behavior")
        assert _is_protected("naumi_agent.safety.permissions")

    def test_protects_config(self):
        assert _is_protected("naumi_agent.config.settings")

    def test_protects_router(self):
        assert _is_protected("naumi_agent.model.router")

    def test_protects_hotreload_itself(self):
        assert _is_protected("naumi_agent.tools.hotreload")

    def test_allows_tools(self):
        assert not _is_protected("naumi_agent.tools.analysis")

    def test_allows_memory(self):
        assert not _is_protected("naumi_agent.memory.long_term")

    def test_allows_unknown(self):
        assert not _is_protected("naumi_agent.some.new.module")


class TestResolveModules:
    def test_all_returns_all_domains(self):
        modules = _resolve_modules("all")
        assert len(modules) >= 20  # all domains combined

    def test_tools_domain(self):
        modules = _resolve_modules("tools")
        assert "naumi_agent.tools.builtin" in modules
        assert "naumi_agent.tools.analysis" in modules
        assert "naumi_agent.tools.self_modify" in modules
        assert "naumi_agent.tools.self_evolve" in modules
        assert "naumi_agent.tools.forge" in modules
        assert "naumi_agent.tools.analysis_support.watchdog" in modules
        assert "naumi_agent.tools.hotreload" not in modules
        assert "naumi_agent.tools.base" not in modules

    def test_memory_domain(self):
        modules = _resolve_modules("memory")
        assert "naumi_agent.memory.long_term" in modules
        assert len(modules) == 3

    def test_skills_domain(self):
        modules = _resolve_modules("skills")
        assert "naumi_agent.skills.skill" in modules

    def test_single_module(self):
        modules = _resolve_modules("naumi_agent.tools.web")
        assert modules == ["naumi_agent.tools.web"]

    def test_rejects_non_agent_module(self):
        with pytest.raises(ValueError, match="naumi_agent"):
            _resolve_modules("os")


class TestNormalizeReloadTarget:
    def test_accepts_domain(self):
        assert _normalize_reload_target(" tools ") == "tools"

    def test_accepts_agent_module(self):
        assert (
            _normalize_reload_target("naumi_agent.tools.web")
            == "naumi_agent.tools.web"
        )

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="不能为空"):
            _normalize_reload_target("")

    def test_rejects_invalid_module_format(self):
        with pytest.raises(ValueError, match="格式无效"):
            _normalize_reload_target("naumi_agent.tools.bad-name")


class TestReloadModule:
    """These tests mutate global module state — run in isolation."""

    @pytest.mark.skip(reason="mutates global module state, run separately")
    def test_reload_loaded_module(self):
        result = reload_module("naumi_agent.tools.builtin")
        assert result["status"] == "reloaded"
        assert "path" in result

    def test_protected_module_returns_protected(self):
        result = reload_module("naumi_agent.orchestrator.engine")
        assert result["status"] == "protected"
        assert "保护区" in result["error"]

    def test_nonexistent_module_returns_not_found(self):
        result = reload_module("naumi_agent.nonexistent.module.xyz")
        assert result["status"] in ("not_found", "error")

    def test_reload_invalid_module(self):
        result = reload_module("not_a_real_module_at_all")
        assert result["status"] == "rejected"

    def test_rejects_domain_target(self):
        result = reload_module("tools")
        assert result["status"] == "rejected"


class TestReloadDomain:
    """These tests mutate global module state — run in isolation."""

    @pytest.mark.skip(reason="mutates global module state, run separately")
    def test_tools_domain(self):
        results = reload_domain("tools")
        assert len(results) > 0
        reloaded = sum(1 for r in results if r["status"] == "reloaded")
        assert reloaded > 0

    @pytest.mark.skip(reason="mutates global module state, run separately")
    def test_invalid_domain_returns_single_result(self):
        results = reload_domain("naumi_agent.nonexistent.module")
        assert len(results) == 1
        assert results[0]["status"] in ("error", "not_found")


class TestListReloadable:
    def test_returns_domain_map(self):
        domains = list_reloadable()
        assert "tools" in domains
        assert "memory" in domains
        assert "skills" in domains
        assert isinstance(domains["tools"], list)

    def test_tools_has_key_modules(self):
        domains = list_reloadable()
        assert "naumi_agent.tools.analysis" in domains["tools"]
        assert "naumi_agent.tools.self_evolve" in domains["tools"]
        assert "naumi_agent.tools.analysis_support.watchdog" in domains["tools"]


class TestHotReloadTool:
    def test_tool_name(self):
        assert HotReloadTool().name == "hot_reload"

    def test_tool_description(self):
        desc = HotReloadTool().description
        assert "热重载" in desc or "reload" in desc.lower()

    def test_tool_schema(self):
        schema = HotReloadTool().parameters_schema
        assert "target" in schema["properties"]
        assert "target" in schema["required"]

    def test_metadata_marks_hot_reload_as_confirmed_state_change(self):
        metadata = HotReloadTool().metadata
        assert metadata.destructive is True
        assert metadata.requires_confirmation is True
        assert metadata.user_facing_name == "热重载"

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="mutates global module state, run separately")
    async def test_execute_tools(self):
        tool = HotReloadTool()
        result = await tool.execute(target="tools")
        assert "热重载" in result
        assert "重载" in result

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="mutates global module state, run separately")
    async def test_execute_all(self):
        tool = HotReloadTool()
        result = await tool.execute(target="all")
        assert "统计" in result

    @pytest.mark.asyncio
    async def test_execute_protected_reports(self):
        tool = HotReloadTool()
        result = await tool.execute(target="naumi_agent.orchestrator.engine")
        assert "受保护" in result or "protected" in result.lower() or "禁止" in result

    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_target(self):
        tool = HotReloadTool()
        result = await tool.execute(target="os")
        assert "已拒绝" in result or "naumi_agent" in result
