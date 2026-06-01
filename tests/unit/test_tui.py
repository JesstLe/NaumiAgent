"""TUI 组件测试."""

import asyncio

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tui.app import NaumiApp, _format_tool_output_markdown


class TestNaumiApp:
    def test_app_creation(self) -> None:
        config = AppConfig()
        engine = AgentEngine(config)
        app = NaumiApp(engine)
        assert app.engine is engine
        assert app.TITLE == "⬡ NaumiAgent"

    def test_bindings_exist(self) -> None:
        config = AppConfig()
        engine = AgentEngine(config)
        app = NaumiApp(engine)
        binding_keys = [b.key for b in app.BINDINGS]
        assert "ctrl+q" in binding_keys
        assert "tab" in binding_keys
        assert "ctrl+l" in binding_keys

    def test_tool_output_markdown_wraps_raw_diff(self) -> None:
        rendered = _format_tool_output_markdown("--- a\n+++ b\n@@\n-old\n+new")

        assert rendered.startswith("```diff")
        assert "-old" in rendered
        assert "+new" in rendered

    def test_tool_output_markdown_preserves_existing_fence(self) -> None:
        rendered = _format_tool_output_markdown("```python\nprint('ok')\n```")

        assert rendered == "```python\nprint('ok')\n```"

    @pytest.mark.asyncio
    async def test_permission_confirmation_modal_returns_choice(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            task = asyncio.create_task(
                app.confirm_permission(
                    {
                        "tool_name": "code_execute",
                        "reason": "该工具需要用户确认。",
                        "arguments": {"code": "print('ok')"},
                        "risk_level": "high",
                        "permission_mode": "moderate",
                    }
                )
            )
            await pilot.pause(0.1)
            await pilot.click("#allow")
            choice = await asyncio.wait_for(task, timeout=2)

        assert choice == "allow"
