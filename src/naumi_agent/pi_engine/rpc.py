"""Async JSONL RPC client for the pi coding agent (`pi --mode rpc`).

The protocol is line-framed JSON over the child process's stdin/stdout:
commands carry a client-generated ``id``; ``response`` records echo it back,
while every other record is a stream event.  See ``PiEventType`` for the
event vocabulary observed against pi 0.85.1.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
from collections import deque
from collections.abc import Callable
from typing import Any

_PI_COMMAND_TIMEOUT_SECONDS = 120.0
_STOP_TIMEOUT_SECONDS = 5.0


class PiRpcError(RuntimeError):
    """A pi RPC command failed or the channel broke."""


class PiEventType:
    """Stream event types emitted by pi in RPC mode (v0.85.1, observed)."""

    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_SETTLED = "agent_settled"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    BASH_EXECUTION_UPDATE = "bash_execution_update"
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    AUTO_RETRY_START = "auto_retry_start"
    AUTO_RETRY_END = "auto_retry_end"
    QUEUE_UPDATE = "queue_update"
    EXTENSION_ERROR = "extension_error"
    EXTENSION_UI_REQUEST = "extension_ui_request"
    RESPONSE = "response"


class PiRpcClient:
    """Own one pi RPC subprocess and expose typed command/event traffic."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._process = process
        self._event_handler = event_handler
        self._id_counter = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._closed = False
        self._reader_dead = False

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    async def start(
        cls,
        *,
        binary: str = "pi",
        provider: str | None = None,
        model: str | None = None,
        extra_args: list[str] | None = None,
        cwd: str | None = None,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> PiRpcClient:
        """Spawn ``pi --mode rpc`` and start pumping its stdout/stderr.

        ``argv`` replaces the whole command line and ``env`` adds variables
        for the child; tests use both to substitute a fake transport or to
        inject a provider key.
        """
        child_env = {**os.environ, **env} if env else None
        if argv is None:
            resolved = shutil.which(binary) or binary
            argv = [resolved, "--mode", "rpc"]
            if provider:
                argv.extend(["--provider", provider])
            if model:
                argv.extend(["--model", model])
            if extra_args:
                argv.extend(extra_args)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )
        except (OSError, FileNotFoundError) as exc:
            raise PiRpcError(
                f"无法启动 pi 引擎进程（{argv[0]}）：{exc}。"
                "请先安装：npm install -g @earendil-works/pi-coding-agent"
            ) from exc
        client = cls(process, event_handler=event_handler)
        client._reader_task = asyncio.get_event_loop().create_task(
            client._read_stdout(), name="pi-rpc-stdout"
        )
        if process.stderr is not None:
            client._stderr_task = asyncio.get_event_loop().create_task(
                client._read_stderr(), name="pi-rpc-stderr"
            )
        return client

    async def stop(self) -> None:
        """Close stdin, wait for exit, then escalate to kill if needed."""
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(PiRpcError("pi RPC 通道已关闭。"))
        self._pending.clear()

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    # -- commands ------------------------------------------------------------

    async def request(
        self,
        command_type: str,
        *,
        timeout: float = _PI_COMMAND_TIMEOUT_SECONDS,
        **fields: Any,
    ) -> dict[str, Any]:
        """Send one command and await its ``response`` record's ``data``."""
        command: dict[str, Any] = {"type": command_type, **fields}
        command_id = f"naumi-{next(self._id_counter)}"
        command["id"] = command_id
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[command_id] = future
        if self._reader_dead:
            raise PiRpcError("pi RPC 通道已关闭，无法发送命令。")
        line = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(line.encode("utf-8") + b"\n")
            await self._process.stdin.drain()
            response = await asyncio.wait_for(future, timeout=timeout)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(command_id, None)
            raise PiRpcError("pi 引擎进程已退出，RPC 通道不可用。") from exc
        except TimeoutError as exc:
            self._pending.pop(command_id, None)
            raise PiRpcError(f"pi 命令 {command_type} 超时（{timeout:.0f}s）。") from exc
        if not response.get("success"):
            message = response.get("error") or "未知错误"
            raise PiRpcError(f"pi 命令 {command_type} 失败：{message}")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def prompt(self, message: str, *, images: list[str] | None = None) -> None:
        """Submit one user prompt; streaming arrives via the event channel."""
        await self.request("prompt", message=message, images=images)

    async def steer(self, message: str) -> None:
        await self.request("steer", message=message)

    async def abort(self) -> None:
        await self.request("abort")

    async def new_session(self) -> None:
        await self.request("new_session")

    async def get_state(self) -> dict[str, Any]:
        return await self.request("get_state")

    async def set_model(self, provider: str, model_id: str) -> dict[str, Any]:
        return await self.request(
            "set_model", provider=provider, modelId=model_id
        )

    async def get_available_models(self) -> list[dict[str, Any]]:
        data = await self.request("get_available_models")
        models = data.get("models")
        return models if isinstance(models, list) else []

    async def compact(self, custom_instructions: str | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if custom_instructions:
            fields["customInstructions"] = custom_instructions
        return await self.request("compact", **fields)

    async def set_session_name(self, name: str) -> None:
        await self.request("set_session_name", name=name)

    async def send_extension_ui_response(
        self,
        request_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Answer an ``extension_ui_request`` raised by pi mid-run.

        pi resolves the pending dialog by reading the answer fields at the
        TOP level of the record (``confirmed`` / ``value`` / ``cancelled``),
        and this record is not a command: it is routed by id with no response
        envelope, so it bypasses the pending-future machinery.
        """
        record: dict[str, Any] = {
            "type": "extension_ui_response",
            "id": request_id,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(line.encode("utf-8") + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PiRpcError("pi 引擎进程已退出，无法应答扩展交互。") from exc

    # -- internal pumps ------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        failure: PiRpcError | None = None
        try:
            while True:
                try:
                    raw = await self._process.stdout.readline()
                except ValueError:
                    # A single JSONL line exceeded the stream buffer limit
                    # (e.g. a giant tool result). Drop that line, keep reading.
                    await self._dispatch_error(
                        PiRpcError(
                            "pi 输出了一条超过 1MB 的单行记录（已跳过该行继续）。"
                        )
                    )
                    continue
                if not raw:
                    break  # EOF: the pi process is gone
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    await self._dispatch_error(
                        PiRpcError(f"pi 输出了无法解析的行（已忽略）：{text[:120]}")
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == PiEventType.RESPONSE:
                    self._resolve_response(record)
                else:
                    await self._dispatch_event(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # channel death must surface to the consumer
            failure = PiRpcError(f"pi RPC 读取失败：{exc}")
        if failure is None:
            code = self._process.returncode
            tail = " | ".join(list(self.stderr_tail)[-2:])
            failure = PiRpcError(
                f"pi 引擎进程已退出（退出码 {code if code is not None else '未知'}）。"
                + (f"stderr 尾部：{tail}" if tail else "")
            )
        self._reader_dead = True
        self._fail_pending(PiRpcError("pi RPC 通道已关闭。"))
        await self._dispatch_error(failure)

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            async for raw in stderr:
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self.stderr_tail.append(text)
        except asyncio.CancelledError:
            raise

    def _resolve_response(self, record: dict[str, Any]) -> None:
        command_id = str(record.get("id") or "")
        future = self._pending.pop(command_id, None)
        if future is not None and not future.done():
            future.set_result(record)

    async def _dispatch_event(self, record: dict[str, Any]) -> None:
        handler = self._event_handler
        if handler is None:
            return
        result = handler(record)
        if asyncio.iscoroutine(result):
            await result

    def _fail_pending(self, error: PiRpcError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(PiRpcError(str(error)))
        self._pending.clear()

    async def _dispatch_error(self, error: PiRpcError) -> None:
        record = {"type": "__rpc_error__", "error": str(error)}
        await self._dispatch_event(record)
