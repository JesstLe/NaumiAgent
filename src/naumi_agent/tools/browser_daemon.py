"""HTTP adapter tools for the external browser-debugging-daemon project."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from naumi_agent.config.settings import BrowserDaemonConfig
from naumi_agent.tools.base import Tool


class BrowserDaemonError(RuntimeError):
    """Raised when the browser daemon HTTP API cannot complete a request."""


RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})
RUN_HANDOFF_STATUSES = frozenset(
    {"waiting_for_instruction", "manual_control_requested", "manual_control"}
)
RUN_WATCH_READY_STATUSES = RUN_TERMINAL_STATUSES | RUN_HANDOFF_STATUSES


class BrowserDaemonClient:
    """Small async client for browser-debugging-daemon's HTTP API."""

    def __init__(self, config: BrowserDaemonConfig, log_dir: Path | None = None) -> None:
        self.config = config
        self._log_dir = log_dir
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return self.config.base_url.rstrip("/")

    @property
    def dashboard_url(self) -> str:
        if not self.config.token:
            return f"{self.base_url}/dashboard"
        return f"{self.base_url}/dashboard?{urlencode({'token': self.config.token})}"

    def _headers(self) -> dict[str, str]:
        if not self.config.token:
            return {}
        return {"Authorization": f"Bearer {self.config.token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise BrowserDaemonError("browser-debugging-daemon 集成已禁用。")

        url = f"{self.base_url}/{path.lstrip('/')}"
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise BrowserDaemonError(
                f"无法连接 browser-debugging-daemon: {exc}. "
                "请先执行 `/bdaemon start` 或手动启动 daemon。"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BrowserDaemonError(
                f"daemon 返回了非 JSON 响应: HTTP {response.status_code}"
            ) from exc

        if response.status_code >= 400:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise BrowserDaemonError(
                f"daemon 请求失败: HTTP {response.status_code}"
                + (f" — {message}" if message else "")
            )
        if not isinstance(payload, dict):
            raise BrowserDaemonError("daemon 返回的 JSON 顶层不是对象。")
        return payload

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/health")

    async def start(self) -> dict[str, Any]:
        try:
            health = await self.health()
            return {"status": "already_running", "health": health}
        except BrowserDaemonError:
            pass

        project_dir = Path(self.config.project_dir).expanduser().resolve()
        scripts_dir = project_dir / "scripts"
        daemon_js = scripts_dir / "daemon.js"
        if not daemon_js.exists():
            raise BrowserDaemonError(
                f"未找到 daemon 入口: {daemon_js}. 请检查 browser_daemon.project_dir。"
            )

        log_path = None
        log_handle = None
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / "daemon.log"
            log_handle = log_path.open("ab")
            stdout = log_handle
            stderr = subprocess.STDOUT

        env = os.environ.copy()
        if self.config.token:
            env["BROWSER_DAEMON_TOKEN"] = self.config.token
        if "BROWSER_DAEMON_PORT" not in env:
            env["BROWSER_DAEMON_PORT"] = self.base_url.rsplit(":", 1)[-1].split("/", 1)[0]

        try:
            self._process = subprocess.Popen(
                ["node", str(daemon_js)],
                cwd=str(scripts_dir),
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        finally:
            if log_handle is not None:
                log_handle.close()

        deadline = asyncio.get_running_loop().time() + self.config.startup_timeout_seconds
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            if self._process.poll() is not None:
                raise BrowserDaemonError(
                    f"daemon 启动后立即退出，退出码 {self._process.returncode}"
                    + (f"，日志: {log_path}" if log_path else "")
                )
            try:
                health = await self.health()
                return {
                    "status": "started",
                    "pid": self._process.pid,
                    "logPath": str(log_path) if log_path else None,
                    "health": health,
                }
            except BrowserDaemonError as exc:
                last_error = str(exc)
                await asyncio.sleep(0.25)

        raise BrowserDaemonError(
            f"daemon 未能在 {self.config.startup_timeout_seconds:.1f}s 内就绪: {last_error}"
            + (f"，日志: {log_path}" if log_path else "")
        )

    async def create_run(
        self,
        task_instruction: str,
        *,
        max_steps: int | None = None,
        browser_source: str = "auto",
        cdp_endpoint: str | None = None,
        handoff_timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "task_instruction": task_instruction,
            "browser_source": browser_source,
        }
        if max_steps is not None:
            body["max_steps"] = max_steps
        if cdp_endpoint:
            body["cdp_endpoint"] = cdp_endpoint
        if handoff_timeout_ms is not None:
            body["handoff_timeout_ms"] = handoff_timeout_ms
        return await self.request("POST", "/runs", json_body=body)

    async def list_runs(self, limit: int = 20) -> dict[str, Any]:
        return await self.request("GET", "/runs", params={"limit": limit})

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/runs/{run_id}")

    async def watch_run(
        self,
        run_id: str,
        *,
        timeout_ms: int = 30_000,
        poll_interval_ms: int = 1_500,
    ) -> dict[str, Any]:
        timeout_ms = _normalize_int(
            timeout_ms,
            fallback=30_000,
            minimum=0,
            maximum=5 * 60 * 1000,
        )
        poll_interval_ms = _normalize_int(
            poll_interval_ms,
            fallback=1_500,
            minimum=200,
            maximum=10_000,
        )
        started_at = asyncio.get_running_loop().time()
        deadline = started_at + (timeout_ms / 1000)
        payload = await self.get_run(run_id)
        run = payload.get("run") or {}

        while not _is_watch_ready(run) and asyncio.get_running_loop().time() < deadline:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.sleep(min(poll_interval_ms / 1000, remaining))
            payload = await self.get_run(run_id)
            run = payload.get("run") or {}

        waited_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
        return {
            "run": run,
            "watch": {
                "runId": run_id,
                "timedOut": not _is_watch_ready(run),
                "waitedMs": waited_ms,
                "readyStatuses": sorted(RUN_WATCH_READY_STATUSES),
            },
        }

    async def reply(self, run_id: str, instruction: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/runs/{run_id}/reply",
            json_body={"instruction": instruction},
        )

    async def resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/runs/{run_id}/resume",
            json_body={"instruction": instruction},
        )

    async def abort(self, run_id: str, reason: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/runs/{run_id}/abort",
            json_body={"reason": reason},
        )

    async def manual_control(self, run_id: str, reason: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/runs/{run_id}/manual-control",
            json_body={"reason": reason},
        )


def _format_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _normalize_int(value: Any, *, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _is_watch_ready(run: dict[str, Any]) -> bool:
    return str(run.get("status") or "") in RUN_WATCH_READY_STATUSES


def _summarize_run(run: dict[str, Any]) -> str:
    run_id = run.get("id", "?")
    status = run.get("status", "?")
    task = run.get("taskInstruction") or run.get("instruction") or ""
    summary = run.get("summary") or ""
    lines = [f"- 运行ID：`{run_id}`", f"- 状态：{status}"]
    if task:
        lines.append(f"- 任务：{task}")
    if summary:
        lines.append(f"- 摘要：{summary}")
    pending = run.get("pendingInput")
    if pending:
        lines.append(f"- 等待输入：`{pending}`")
    return "\n".join(lines)


class BrowserDaemonHealthTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_health"

    @property
    def description(self) -> str:
        return "检查外部 browser-debugging-daemon 的 HTTP 健康状态。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            health = await self._client.health()
        except BrowserDaemonError as exc:
            return f"❌ {exc}\n\nDashboard: {self._client.dashboard_url}"
        return "## browser-debugging-daemon 健康状态\n\n" + _format_json(health)


class BrowserDaemonStartTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_start"

    @property
    def description(self) -> str:
        return "启动本机 workspace 下的 browser-debugging-daemon HTTP 服务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._client.start()
        except BrowserDaemonError as exc:
            return f"❌ 启动失败：{exc}"
        status = result.get("status", "started")
        return (
            f"✅ browser-debugging-daemon {status}\n\n"
            f"- Dashboard: {self._client.dashboard_url}\n"
            f"- PID: {result.get('pid', '已有进程')}\n"
            f"- 日志: {result.get('logPath') or '未写入日志文件'}"
        )


class BrowserDaemonDashboardTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_dashboard"

    @property
    def description(self) -> str:
        return "返回 browser-debugging-daemon dashboard URL。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return self._client.dashboard_url


class BrowserDaemonRunTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_run"

    @property
    def description(self) -> str:
        return "向 browser-debugging-daemon 队列提交一个自主浏览器任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_instruction": {"type": "string", "description": "浏览器任务描述"},
                "max_steps": {"type": "integer", "minimum": 1, "description": "最大步骤数"},
                "browser_source": {
                    "type": "string",
                    "enum": ["auto", "managed", "attached"],
                    "description": "浏览器来源，默认 auto",
                },
                "cdp_endpoint": {"type": "string", "description": "attached 模式 CDP 地址"},
                "handoff_timeout_ms": {"type": "integer", "minimum": 1000},
            },
            "required": ["task_instruction"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task = str(kwargs.get("task_instruction") or "").strip()
        if not task:
            return "❌ task_instruction 不能为空。"
        browser_source = str(kwargs.get("browser_source") or "auto")
        if browser_source not in {"auto", "managed", "attached"}:
            return "❌ browser_source 必须是 auto、managed 或 attached。"
        try:
            payload = await self._client.create_run(
                task,
                max_steps=kwargs.get("max_steps"),
                browser_source=browser_source,
                cdp_endpoint=kwargs.get("cdp_endpoint"),
                handoff_timeout_ms=kwargs.get("handoff_timeout_ms"),
            )
        except BrowserDaemonError as exc:
            return f"❌ 创建运行失败：{exc}"
        run = payload.get("run") or {}
        return (
            "## 已提交 browser-debugging-daemon 运行\n\n"
            f"{_summarize_run(run)}\n"
            f"- Dashboard：{self._client.dashboard_url}"
        )


class BrowserDaemonListRunsTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_list_runs"

    @property
    def description(self) -> str:
        return "列出 browser-debugging-daemon 最近的队列运行。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "default": 20}},
        }

    async def execute(self, **kwargs: Any) -> str:
        limit = int(kwargs.get("limit") or 20)
        try:
            payload = await self._client.list_runs(limit=limit)
        except BrowserDaemonError as exc:
            return f"❌ 列表获取失败：{exc}"
        runs = payload.get("runs") or []
        if not runs:
            return "暂无 browser-debugging-daemon 运行。"
        lines = ["## browser-debugging-daemon 运行列表"]
        for run in runs:
            lines.append("")
            lines.append(_summarize_run(run))
        return "\n".join(lines)


class BrowserDaemonRunStatusTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_run_status"

    @property
    def description(self) -> str:
        return "查看 browser-debugging-daemon 指定运行的详情。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = str(kwargs.get("run_id") or "").strip()
        if not run_id:
            return "❌ run_id 不能为空。"
        try:
            payload = await self._client.get_run(run_id)
        except BrowserDaemonError as exc:
            return f"❌ 运行查询失败：{exc}"
        run = payload.get("run") or {}
        return "## browser-debugging-daemon 运行详情\n\n" + _summarize_run(run)


class BrowserDaemonWatchTool(Tool):
    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "browser_daemon_watch"

    @property
    def description(self) -> str:
        return (
            "等待 browser-debugging-daemon 运行到完成、失败、中止或需要人工接管/回复的状态。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 300000,
                    "default": 30000,
                    "description": "最长等待毫秒数，0 表示只查询一次。",
                },
                "poll_interval_ms": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 10000,
                    "default": 1500,
                    "description": "轮询间隔毫秒数。",
                },
            },
            "required": ["run_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = str(kwargs.get("run_id") or "").strip()
        if not run_id:
            return "❌ run_id 不能为空。"
        timeout_ms = _normalize_int(
            kwargs.get("timeout_ms", 30_000),
            fallback=30_000,
            minimum=0,
            maximum=300_000,
        )
        poll_interval_ms = _normalize_int(
            kwargs.get("poll_interval_ms", 1_500),
            fallback=1_500,
            minimum=200,
            maximum=10_000,
        )
        try:
            payload = await self._client.watch_run(
                run_id,
                timeout_ms=timeout_ms,
                poll_interval_ms=poll_interval_ms,
            )
        except BrowserDaemonError as exc:
            return f"❌ 等待运行失败：{exc}"

        run = payload.get("run") or {}
        watch = payload.get("watch") or {}
        status_line = "已到达可处理状态" if not watch.get("timedOut") else "等待超时"
        return (
            f"## browser-debugging-daemon 运行等待：{status_line}\n\n"
            f"{_summarize_run(run)}\n"
            f"- 已等待：{watch.get('waitedMs', 0)}ms\n"
            f"- 超时：{'是' if watch.get('timedOut') else '否'}\n"
            f"- 可处理状态：{', '.join(watch.get('readyStatuses') or [])}"
        )


class _RunControlTool(Tool):
    endpoint_name = ""
    verb = ""
    default_text = ""
    text_field = "instruction"

    def __init__(self, client: BrowserDaemonClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return f"browser_daemon_{self.endpoint_name}"

    @property
    def description(self) -> str:
        return f"对 browser-debugging-daemon 运行执行 {self.verb} 操作。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                self.text_field: {"type": "string"},
            },
            "required": ["run_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = str(kwargs.get("run_id") or "").strip()
        text = str(kwargs.get(self.text_field) or self.default_text).strip()
        if not run_id:
            return "❌ run_id 不能为空。"
        try:
            payload = await self._execute_control(run_id, text)
        except BrowserDaemonError as exc:
            return f"❌ {self.verb}失败：{exc}"
        return f"## {self.verb}完成\n\n" + _summarize_run(payload.get("run") or {})

    async def _execute_control(self, run_id: str, text: str) -> dict[str, Any]:
        raise BrowserDaemonError("控制工具未配置执行端点。")


class BrowserDaemonReplyTool(_RunControlTool):
    endpoint_name = "reply"
    verb = "回复"
    text_field = "instruction"

    async def _execute_control(self, run_id: str, text: str) -> dict[str, Any]:
        if not text:
            raise BrowserDaemonError("instruction 不能为空。")
        return await self._client.reply(run_id, text)


class BrowserDaemonResumeTool(_RunControlTool):
    endpoint_name = "resume"
    verb = "恢复"
    default_text = "Manual control complete. Continue from the current page."
    text_field = "instruction"

    async def _execute_control(self, run_id: str, text: str) -> dict[str, Any]:
        return await self._client.resume(run_id, text)


class BrowserDaemonAbortTool(_RunControlTool):
    endpoint_name = "abort"
    verb = "中止"
    default_text = "Run aborted by NaumiAgent operator."
    text_field = "reason"

    async def _execute_control(self, run_id: str, text: str) -> dict[str, Any]:
        return await self._client.abort(run_id, text)


class BrowserDaemonManualControlTool(_RunControlTool):
    endpoint_name = "manual_control"
    verb = "请求人工接管"
    default_text = "Manual control requested by NaumiAgent operator."
    text_field = "reason"

    async def _execute_control(self, run_id: str, text: str) -> dict[str, Any]:
        return await self._client.manual_control(run_id, text)


def create_browser_daemon_tools(client: BrowserDaemonClient) -> list[Tool]:
    return [
        BrowserDaemonHealthTool(client),
        BrowserDaemonStartTool(client),
        BrowserDaemonDashboardTool(client),
        BrowserDaemonRunTool(client),
        BrowserDaemonListRunsTool(client),
        BrowserDaemonRunStatusTool(client),
        BrowserDaemonWatchTool(client),
        BrowserDaemonReplyTool(client),
        BrowserDaemonResumeTool(client),
        BrowserDaemonAbortTool(client),
        BrowserDaemonManualControlTool(client),
    ]
