"""Tests for browser-debugging-daemon HTTP adapter tools."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import BrowserDaemonConfig
from naumi_agent.tools.browser_daemon import (
    BrowserDaemonClient,
    BrowserDaemonRunTool,
    BrowserDaemonStartTool,
    create_browser_daemon_tools,
)


class FakeBrowserDaemonClient:
    dashboard_url = "http://127.0.0.1:3005/dashboard"

    def __init__(self) -> None:
        self.created: dict | None = None

    async def create_run(self, task_instruction: str, **kwargs):
        self.created = {"task_instruction": task_instruction, **kwargs}
        return {
            "status": "ok",
            "run": {
                "id": "run-1",
                "status": "queued",
                "taskInstruction": task_instruction,
                "summary": "",
            },
        }


def test_dashboard_url_includes_token() -> None:
    client = BrowserDaemonClient(
        BrowserDaemonConfig(
            base_url="http://127.0.0.1:3005/",
            token="secret token",
        )
    )

    assert client.dashboard_url == "http://127.0.0.1:3005/dashboard?token=secret+token"


def test_create_browser_daemon_tools() -> None:
    client = BrowserDaemonClient(BrowserDaemonConfig())

    names = {tool.name for tool in create_browser_daemon_tools(client)}

    assert "browser_daemon_health" in names
    assert "browser_daemon_start" in names
    assert "browser_daemon_run" in names
    assert "browser_daemon_manual_control" in names


@pytest.mark.asyncio
async def test_run_tool_submits_task() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonRunTool(client)  # type: ignore[arg-type]

    result = await tool.execute(task_instruction="打开 example.com", browser_source="attached")

    assert "run-1" in result
    assert client.created == {
        "task_instruction": "打开 example.com",
        "max_steps": None,
        "browser_source": "attached",
        "cdp_endpoint": None,
        "handoff_timeout_ms": None,
    }


@pytest.mark.asyncio
async def test_run_tool_rejects_invalid_source() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonRunTool(client)  # type: ignore[arg-type]

    result = await tool.execute(task_instruction="打开 example.com", browser_source="bad")

    assert "browser_source" in result
    assert client.created is None


@pytest.mark.asyncio
async def test_start_tool_reports_missing_project(tmp_path) -> None:
    client = BrowserDaemonClient(
        BrowserDaemonConfig(
            base_url="http://127.0.0.1:1",
            project_dir=str(tmp_path),
            request_timeout_seconds=0.1,
        )
    )
    tool = BrowserDaemonStartTool(client)

    result = await tool.execute()

    assert "启动失败" in result
    assert "未找到 daemon 入口" in result
