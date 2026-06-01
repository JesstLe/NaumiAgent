"""Tests for browser-debugging-daemon HTTP adapter tools."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import BrowserDaemonConfig
from naumi_agent.tools.browser_daemon import (
    BrowserDaemonClient,
    BrowserDaemonListRunsTool,
    BrowserDaemonReplyTool,
    BrowserDaemonRunTool,
    BrowserDaemonStartTool,
    BrowserDaemonWatchTool,
    create_browser_daemon_tools,
)


class FakeBrowserDaemonClient:
    dashboard_url = "http://127.0.0.1:3005/dashboard"

    def __init__(self) -> None:
        self.created: dict | None = None
        self.replies: list[dict] = []
        self.watch_args: dict | None = None
        self.list_limit: int | None = None

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

    async def reply(self, run_id: str, instruction: str):
        self.replies.append({"run_id": run_id, "instruction": instruction})
        return {
            "run": {
                "id": run_id,
                "status": "queued",
                "taskInstruction": "继续任务",
                "summary": "已接收回复",
            },
        }

    async def watch_run(self, run_id: str, **kwargs):
        self.watch_args = {"run_id": run_id, **kwargs}
        return {
            "run": {
                "id": run_id,
                "status": "manual_control",
                "taskInstruction": "登录后台",
                "pendingInput": {"mode": "manual_control"},
            },
            "watch": {
                "runId": run_id,
                "timedOut": False,
                "waitedMs": 12,
                "readyStatuses": ["aborted", "completed", "manual_control"],
            },
        }

    async def list_runs(self, limit: int):
        self.list_limit = limit
        return {"runs": []}


class PollingBrowserDaemonClient(BrowserDaemonClient):
    def __init__(self, statuses: list[str]) -> None:
        super().__init__(BrowserDaemonConfig())
        self.statuses = statuses
        self.calls = 0

    async def get_run(self, run_id: str):
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return {
            "run": {
                "id": run_id,
                "status": status,
                "taskInstruction": "轮询任务",
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

    tools = create_browser_daemon_tools(client)
    names = {tool.name for tool in tools}

    assert "browser_daemon_health" in names
    assert "browser_daemon_start" in names
    assert "browser_daemon_run" in names
    assert "browser_daemon_watch" in names
    assert "browser_daemon_manual_control" in names


def test_browser_daemon_tools_expose_permission_metadata() -> None:
    client = BrowserDaemonClient(BrowserDaemonConfig())
    tools = {tool.name: tool for tool in create_browser_daemon_tools(client)}

    assert tools["browser_daemon_health"].metadata.read_only is True
    assert tools["browser_daemon_list_runs"].metadata.read_only is True
    assert tools["browser_daemon_run_status"].metadata.concurrency_safe is True
    assert tools["browser_daemon_start"].metadata.requires_confirmation is True
    assert tools["browser_daemon_run"].metadata.requires_confirmation is True
    assert tools["browser_daemon_abort"].metadata.destructive is True


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
async def test_run_tool_clamps_numeric_inputs() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonRunTool(client)  # type: ignore[arg-type]

    result = await tool.execute(
        task_instruction="打开 example.com",
        max_steps=999_999,
        handoff_timeout_ms=999_999_999,
        cdp_endpoint="  ws://localhost:9222/devtools/browser/1  ",
    )

    assert "run-1" in result
    assert client.created is not None
    assert client.created["max_steps"] == 500
    assert client.created["handoff_timeout_ms"] == 600_000
    assert client.created["cdp_endpoint"] == "ws://localhost:9222/devtools/browser/1"


@pytest.mark.asyncio
async def test_run_tool_rejects_overlong_task() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonRunTool(client)  # type: ignore[arg-type]

    result = await tool.execute(task_instruction="x" * 8001)

    assert "task_instruction 过长" in result
    assert client.created is None


@pytest.mark.asyncio
async def test_run_tool_rejects_invalid_source() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonRunTool(client)  # type: ignore[arg-type]

    result = await tool.execute(task_instruction="打开 example.com", browser_source="bad")

    assert "browser_source" in result
    assert client.created is None


@pytest.mark.asyncio
async def test_watch_tool_reports_handoff_ready_state() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonWatchTool(client)  # type: ignore[arg-type]

    result = await tool.execute(run_id="run-1", timeout_ms=1000, poll_interval_ms=200)

    assert "已到达可处理状态" in result
    assert "manual_control" in result
    assert "已等待：12ms" in result
    assert client.watch_args == {
        "run_id": "run-1",
        "timeout_ms": 1000,
        "poll_interval_ms": 200,
    }


@pytest.mark.asyncio
async def test_list_runs_tool_clamps_limit() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonListRunsTool(client)  # type: ignore[arg-type]

    result = await tool.execute(limit=999)

    assert "暂无 browser-debugging-daemon 运行" in result
    assert client.list_limit == 100


@pytest.mark.asyncio
async def test_client_watch_run_returns_immediately_for_ready_status() -> None:
    client = PollingBrowserDaemonClient(["completed"])

    payload = await client.watch_run("run-1", timeout_ms=0)

    assert payload["watch"]["timedOut"] is False
    assert payload["run"]["status"] == "completed"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_client_watch_run_times_out_for_non_ready_status() -> None:
    client = PollingBrowserDaemonClient(["running"])

    payload = await client.watch_run("run-1", timeout_ms=0)

    assert payload["watch"]["timedOut"] is True
    assert payload["run"]["status"] == "running"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_reply_tool_rejects_empty_instruction() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonReplyTool(client)  # type: ignore[arg-type]

    result = await tool.execute(run_id="run-1", instruction="")

    assert "instruction 不能为空" in result
    assert client.replies == []


@pytest.mark.asyncio
async def test_reply_tool_uses_client_reply() -> None:
    client = FakeBrowserDaemonClient()
    tool = BrowserDaemonReplyTool(client)  # type: ignore[arg-type]

    result = await tool.execute(run_id="run-1", instruction="继续点击保存")

    assert "回复完成" in result
    assert "已接收回复" in result
    assert client.replies == [{"run_id": "run-1", "instruction": "继续点击保存"}]


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
