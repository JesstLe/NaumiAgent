"""Terminal UI JSONL bridge backed by the pi coding agent.

Speaks the exact protocol the Node terminal UI already speaks (see
``naumi_agent.ui.protocol``) so the frontend needs no changes: hello
negotiation, typed UI messages, run lifecycle, and permission bubbles.
Only the capabilities actually provided are advertised; every other
surface degrades gracefully on the client side.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from naumi_agent.pi_engine.rpc import PiEventType, PiRpcClient, PiRpcError
from naumi_agent.pi_engine.session import PiSessionMapper
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.ui.messages.base import MessageType
from naumi_agent.ui.messages.events import SystemNoticeMessage
from naumi_agent.ui.permission_confirmation import public_permission_request_payload
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    negotiate_hello,
    normalize_client_record,
    ui_message_payload,
)
from naumi_agent.ui.protocol_registry import load_protocol_event_registry

# Capabilities this bridge can honestly provide.  Every event it emits is a
# core (unbound) protocol event, so no optional capability is required.
PI_BRIDGE_CAPABILITIES = ("typed_ui_messages",)

_BRIDGE_SLASH_COMMANDS = (
    "/help",
    "/engine",
    "/model",
    "/models",
    "/new",
    "/compact",
)

_MODE_NOTICE_EMITTED_ONCE = "pi 引擎不使用 Naumi 运行模式，模式切换仅更新界面显示。"


def _notice(title: str, content: str, level: str = "info") -> dict[str, Any]:
    return ui_message_payload(
        SystemNoticeMessage(
            type=MessageType.SYSTEM_NOTICE, title=title, content=content, level=level
        )
    )


# UIMessage payload fields that may carry model or tool output text and are
# marked sensitive by the protocol registry (redaction: required).
_REDACTABLE_PAYLOAD_FIELDS = (
    "content",
    "content_preview",
    "message",
    "args_raw",
    "args_summary",
    "primary_arg",
    "command",
    "query",
)


def _redact_ui_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in _REDACTABLE_PAYLOAD_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            payload[key] = OutputGuardrail.redact(value)
    return payload


class PiBridgeError(RuntimeError):
    """User-visible pi bridge failure (message is safe to display)."""


class _PendingPiPermission:
    __slots__ = ("pi_request_id", "method", "future")

    def __init__(self, pi_request_id: str, method: str) -> None:
        self.pi_request_id = pi_request_id
        self.method = method
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()


class PiTerminalBridge:
    """Serve the terminal UI protocol on top of one pi RPC subprocess."""

    def __init__(
        self,
        *,
        binary: str = "pi",
        provider: str | None = None,
        model: str | None = None,
        extra_args: list[str] | None = None,
        workspace_root: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._provider = provider
        self._model = model
        self._extra_args = list(extra_args or [])
        self._workspace_root = workspace_root or Path.cwd()
        self._env = dict(env or {})
        self._registry = load_protocol_event_registry()
        self._sequence = 0
        self._writer: TextIO | None = None
        self._writer_lock = asyncio.Lock()
        self._negotiated = False
        self._closed = False
        self._rpc: PiRpcClient | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._mapper = PiSessionMapper()
        self._run_settled = asyncio.Event()
        self._run_active = asyncio.Event()
        self._running = False
        self._cancel_requested = False
        self._run_request_id = ""
        self._run_task: asyncio.Task[None] | None = None
        self._pending_permissions: dict[str, _PendingPiPermission] = {}
        self._mode_notice_emitted = False
        self._state_cache: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, rpc: PiRpcClient | None = None) -> None:
        """Spawn pi and begin pumping its event stream.

        ``rpc`` allows tests to substitute a fake transport; production
        callers leave it as ``None`` to spawn a real pi subprocess.
        """
        if rpc is not None:
            self._rpc = rpc
            self._event_queue = getattr(rpc, "event_queue", self._event_queue)
        else:
            self._rpc = await PiRpcClient.start(
                binary=self._binary,
                provider=self._provider,
                model=self._model,
                extra_args=self._extra_args,
                cwd=str(self._workspace_root),
                event_handler=self._event_queue.put_nowait,
                env=self._env or None,
            )
        self._consumer_task = asyncio.get_event_loop().create_task(
            self._consume_pi_events(), name="pi-bridge-consumer"
        )
        await self._refresh_state()

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
        if self._rpc is not None:
            await self._rpc.stop()

    def bind_writer(self, writer: TextIO) -> None:
        self._writer = writer

    # -- protocol plumbing ----------------------------------------------------

    async def emit(
        self,
        event: ServerEventType | str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        """Write one registry-governed envelope to the UI."""
        if self._writer is None:
            raise PiBridgeError("bridge writer 尚未绑定。")
        policy = self._registry.policy("server", str(event))
        async with self._writer_lock:
            self._sequence += 1
            envelope = make_envelope(
                event,
                payload or {},
                request_id=request_id,
                sequence=self._sequence,
                criticality=policy.criticality,
            )
            self._writer.write(encode_jsonl(envelope))
            self._writer.flush()

    async def emit_error(
        self,
        message: str,
        *,
        code: str = "pi_bridge_error",
        request_id: str | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.ERROR, {"error": message, "code": code}, request_id=request_id
        )

    async def emit_ready(self) -> None:
        await self.emit(ServerEventType.READY, self.status_payload())

    def status_payload(self) -> dict[str, Any]:
        model = self._state_cache.get("model") or {}
        return {
            "engine": "pi",
            "provider": str(model.get("provider") or self._provider or ""),
            "model": str(model.get("id") or self._model or ""),
            "model_name": str(model.get("name") or model.get("id") or ""),
            "mode": "default",
            "session_id": str(self._state_cache.get("sessionId") or ""),
            "workspace_root": str(self._workspace_root),
            "slash_commands": list(_BRIDGE_SLASH_COMMANDS),
            "running": self._running,
        }

    async def _refresh_state(self) -> None:
        if self._rpc is None:
            return
        try:
            self._state_cache = await self._rpc.get_state()
        except PiRpcError as exc:
            await self.emit_error(f"获取 pi 引擎状态失败：{exc}")
            return
        await self.emit(ServerEventType.STATUS, self.status_payload())

    # -- client event dispatch --------------------------------------------------

    async def handle_client_record(self, record: dict[str, Any]) -> None:
        normalized = normalize_client_record(record)
        event_type = ClientEventType(normalized["type"])
        payload = normalized.get("payload") or {}
        request_id = str(normalized.get("request_id") or "") or None
        handler: Callable[..., Any] | None = _CLIENT_HANDLERS.get(event_type)
        if handler is None:
            await self.emit_error(
                f"pi 引擎当前不支持命令 {event_type}。可使用 /engine 查看切换方式。",
                code="pi_bridge_unsupported_command",
                request_id=request_id,
            )
            return
        await handler(self, payload, request_id)

    async def _handle_hello(self, payload: dict[str, Any], request_id: str | None) -> None:
        from naumi_agent.ui.protocol import ProtocolNegotiationError

        try:
            negotiation = negotiate_hello(payload)
        except ProtocolNegotiationError as exc:
            await self.emit_error(str(exc), code=exc.code, request_id=request_id)
            return
        self._negotiated = True
        await self.emit(
            ServerEventType.ACK,
            {"event": "hello", "negotiation": negotiation},
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def _handle_ping(self, _: dict[str, Any], request_id: str | None) -> None:
        await self.emit(ServerEventType.PONG, {}, request_id=request_id)

    async def _handle_shutdown(self, _: dict[str, Any], request_id: str | None) -> None:
        await self.emit(ServerEventType.ACK, {"ok": True}, request_id=request_id)
        await self.emit(ServerEventType.SHUTDOWN, {})
        await self.shutdown()

    async def _handle_submit(self, payload: dict[str, Any], request_id: str | None) -> None:
        text = str(payload.get("text") or "")
        if not text.strip():
            await self.emit_error("提交内容不能为空。", request_id=request_id)
            return
        if self._running:
            await self.emit_error(
                "pi 引擎正在执行上一条任务，请等待完成或先取消。",
                code="pi_bridge_busy",
                request_id=request_id,
            )
            return
        command = _match_bridge_slash(text)
        if command is not None:
            await self._run_bridge_slash(command, text, request_id)
            return
        # Mark busy synchronously and run the conversation as its own task so
        # ping / run_cancel / permission_response stay answerable mid-run.
        self._running = True
        self._run_active.clear()
        self._run_task = asyncio.get_event_loop().create_task(
            self._run_conversation(text, request_id)
        )

    async def _handle_run_cancel(
        self, payload: dict[str, Any], request_id: str | None
    ) -> None:
        if not self._running or self._rpc is None:
            await self.emit(
                ServerEventType.RUN_CANCELLED, {"reason": "没有正在运行的任务。"},
                request_id=request_id,
            )
            return
        reason = str(payload.get("reason") or "用户取消。")
        self._cancel_requested = True
        try:
            await self._rpc.abort()
        except PiRpcError as exc:
            self._cancel_requested = False
            await self.emit_error(f"取消 pi 任务失败：{exc}", request_id=request_id)
            return
        try:
            await asyncio.wait_for(self._run_settled.wait(), timeout=30)
        except TimeoutError:
            pass
        self._running = False
        await self.emit(
            ServerEventType.RUN_CANCELLED, {"reason": reason}, request_id=request_id
        )
        await self._refresh_state()

    async def _handle_set_mode(self, payload: dict[str, Any], _: str | None) -> None:
        if not self._mode_notice_emitted:
            self._mode_notice_emitted = True
            await self.emit(
                ServerEventType.UI_MESSAGE, _notice("运行模式", _MODE_NOTICE_EMITTED_ONCE)
            )
        await self.emit(
            ServerEventType.MODE_CHANGED, {"mode": str(payload.get("mode") or "default")}
        )

    async def _handle_cycle_mode(self, _: dict[str, Any], __: str | None) -> None:
        if not self._mode_notice_emitted:
            self._mode_notice_emitted = True
            await self.emit(
                ServerEventType.UI_MESSAGE, _notice("运行模式", _MODE_NOTICE_EMITTED_ONCE)
            )

    async def _handle_permission_response(
        self, payload: dict[str, Any], _: str | None
    ) -> None:
        request_id = str(payload.get("request_id") or "")
        choice = str(payload.get("choice") or "")
        pending = self._pending_permissions.pop(request_id, None)
        if pending is None:
            await self.emit_error(
                f"未找到待处理的权限请求：{request_id or '(空)'}",
                code="pi_bridge_permission_unknown",
            )
            return
        if not pending.future.done():
            pending.future.set_result(choice)
        await self.emit(
            ServerEventType.PERMISSION_RESOLVED,
            {
                "request_id": request_id,
                "choice": choice,
                "resolved_by": "user",
            },
        )

    # -- conversation -----------------------------------------------------------

    async def _run_conversation(self, text: str, request_id: str | None) -> None:
        assert self._rpc is not None
        self._mapper.reset()
        self._run_settled.clear()
        self._cancel_requested = False
        self._run_request_id = request_id or ""
        # Only now may the consumer feed run-scoped events into the mapper.
        self._run_active.set()
        try:
            await self._drive_conversation(text, request_id)
        except asyncio.CancelledError:
            self._running = False
            self._run_active.clear()
            return

    async def _drive_conversation(self, text: str, request_id: str | None) -> None:
        redacted = OutputGuardrail.redact(text)
        await self.emit(
            ServerEventType.USER_MESSAGE, {"content": redacted}, request_id=request_id
        )
        await self.emit(
            ServerEventType.RUN_STARTED, {"task": redacted}, request_id=request_id
        )
        await self._refresh_state()
        try:
            await self._rpc.prompt(text)
        except PiRpcError as exc:
            self._running = False
            self._run_active.clear()
            await self.emit(
                ServerEventType.RUN_COMPLETED,
                {"status": "error", "response": "", "error": str(exc)},
                request_id=request_id,
            )
            return
        try:
            await asyncio.wait_for(self._run_settled.wait(), timeout=3600)
        except TimeoutError:
            await self._rpc.abort()
            self._running = False
            self._run_active.clear()
            await self.emit_error("pi 任务超时（60 分钟），已自动中止。", request_id=request_id)
            return
        self._running = False
        self._run_active.clear()
        if self._cancel_requested:
            # The run_cancel handler owns the terminal event for this run.
            return
        status = "error" if self._mapper.error else "completed"
        await self.emit(
            ServerEventType.RUN_COMPLETED,
            {
                "status": status,
                "response": OutputGuardrail.redact(self._mapper.final_text),
                "error": OutputGuardrail.redact(self._mapper.error),
            },
            request_id=request_id,
        )
        await self._refresh_state()

    # -- pi event consumption -----------------------------------------------------

    async def _consume_pi_events(self) -> None:
        while not self._closed:
            event = await self._event_queue.get()
            kind = str(event.get("type") or "")
            if kind == "__rpc_error__":
                await self.emit_error(str(event.get("error") or "pi RPC 通道错误。"))
                if self._running:
                    self._run_settled.set()
                continue
            if kind == PiEventType.EXTENSION_UI_REQUEST:
                await self._handle_extension_ui_request(event)
                continue
            if not self._running:
                continue
            # Wait until the run coroutine has reset the mapper so events
            # can never overtake the user-message echo.
            await self._run_active.wait()
            for message in self._mapper.feed(event):
                await self.emit(
                    ServerEventType.UI_MESSAGE,
                    _redact_ui_payload(ui_message_payload(message)),
                )
            if self._mapper.settled:
                self._run_settled.set()

    async def _handle_extension_ui_request(self, event: dict[str, Any]) -> None:
        assert self._rpc is not None
        pi_request_id = str(event.get("id") or "")
        method = str(event.get("method") or "")
        if method in {"notify", "setStatus", "setTitle", "setWidget"}:
            return  # fire-and-forget decoration; nothing to answer
        if not pi_request_id:
            return
        if method not in {"confirm", "select", "input"}:
            await self._rpc.send_extension_ui_response(
                pi_request_id, {"cancelled": True}
            )
            return
        if method != "confirm":
            # The terminal permission bubble only carries allow/deny; answer
            # honestly instead of fabricating a choice for select/input.
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice(
                    "pi 扩展交互",
                    "pi 扩展发起了选择/输入对话框，当前终端 UI 仅支持允许/拒绝，"
                    "已代为取消该对话框。",
                    level="warning",
                ),
            )
            await self._rpc.send_extension_ui_response(
                pi_request_id, {"cancelled": True}
            )
            return
        request_id = f"pi-perm-{pi_request_id}"
        pending = _PendingPiPermission(pi_request_id, method)
        self._pending_permissions[request_id] = pending
        public_payload = public_permission_request_payload(
            {
                "call_id": pi_request_id,
                "tool_name": "pi 扩展确认",
                "arguments": {"message": str(event.get("message") or "")},
                "reason": str(event.get("title") or "pi 扩展请求确认"),
                "risk_level": "medium",
            },
            request_id=request_id,
            choices=("allow_once", "deny"),
        )
        await self.emit(
            ServerEventType.PERMISSION_REQUEST, public_payload, request_id=request_id
        )
        choice = await pending.future
        if choice == "deny":
            await self._rpc.send_extension_ui_response(
                pi_request_id, {"cancelled": True}
            )
            return
        await self._rpc.send_extension_ui_response(
            pi_request_id, {"confirmed": True}
        )

    # -- bridge slash commands -----------------------------------------------------

    async def _run_bridge_slash(
        self, command: str, text: str, request_id: str | None
    ) -> None:
        assert self._rpc is not None
        parts = text.split()
        if command == "/help":
            lines = [
                "pi 引擎模式下的可用命令：",
                *[
                    f"  {name}"
                    for name in _BRIDGE_SLASH_COMMANDS
                    if name != "/help"
                ],
                "  其他 / 开头内容会作为普通消息发送给模型。",
                "  切换回 NaumiAgent 引擎：退出后运行 `naumi --engine naumi`。",
            ]
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice("帮助", "\n".join(lines)),
                request_id=request_id,
            )
        elif command == "/engine":
            model = self._state_cache.get("model") or {}
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice(
                    "当前引擎",
                    "engine=pi（pi coding agent）\n"
                    f"provider={model.get('provider', '?')} model={model.get('id', '?')}\n"
                    "session_id=" + str(self._state_cache.get("sessionId") or "?") +
                    "\n切换回 naumi 引擎：`naumi --engine naumi`"
                    "（或在配置 engine.provider 中修改）。",
                ),
                request_id=request_id,
            )
        elif command == "/model":
            await self._slash_model(parts, request_id)
        elif command == "/models":
            await self._slash_models(request_id)
        elif command == "/new":
            try:
                await self._rpc.new_session()
            except PiRpcError as exc:
                await self.emit_error(f"新建 pi 会话失败：{exc}", request_id=request_id)
                return
            await self._refresh_state()
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice("新会话", "已让 pi 引擎开启新会话。", "success"),
                request_id=request_id,
            )
        elif command == "/compact":
            try:
                await self._rpc.compact()
            except PiRpcError as exc:
                await self.emit_error(f"pi 上下文压缩失败：{exc}", request_id=request_id)
                return
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice("上下文压缩", "pi 上下文压缩已完成。", "success"),
                request_id=request_id,
            )

    async def _slash_model(self, parts: list[str], request_id: str | None) -> None:
        assert self._rpc is not None
        if len(parts) < 2:
            model = self._state_cache.get("model") or {}
            await self.emit(
                ServerEventType.UI_MESSAGE,
                _notice(
                    "当前模型",
                    f"provider={model.get('provider', '?')} model={model.get('id', '?')}\n"
                    "用法：/model <provider>/<model_id>，例如 /model zai-coding-cn/glm-4.7",
                ),
                request_id=request_id,
            )
            return
        target = parts[1].strip()
        provider, _, model_id = target.partition("/")
        if not model_id:
            await self.emit_error(
                "用法：/model <provider>/<model_id>，例如 /model zai-coding-cn/glm-4.7。",
                request_id=request_id,
            )
            return
        try:
            await self._rpc.set_model(provider, model_id)
        except PiRpcError as exc:
            await self.emit_error(f"切换模型失败：{exc}", request_id=request_id)
            return
        await self._refresh_state()
        await self.emit(
            ServerEventType.UI_MESSAGE,
            _notice("模型已切换", f"{provider}/{model_id}", "success"),
            request_id=request_id,
        )

    async def _slash_models(self, request_id: str | None) -> None:
        assert self._rpc is not None
        try:
            models = await self._rpc.get_available_models()
        except PiRpcError as exc:
            await self.emit_error(f"获取模型列表失败：{exc}", request_id=request_id)
            return
        by_provider: dict[str, list[str]] = {}
        for model in models:
            provider = str(model.get("provider") or "?")
            by_provider.setdefault(provider, []).append(str(model.get("id") or "?"))
        lines = [f"共 {len(models)} 个可用模型："]
        for provider in sorted(by_provider):
            ids = by_provider[provider]
            preview = ", ".join(ids[:8]) + ("…" if len(ids) > 8 else "")
            lines.append(f"  {provider}: {preview}")
        await self.emit(
            ServerEventType.UI_MESSAGE,
            _notice("可用模型", "\n".join(lines)),
            request_id=request_id,
        )


def _match_bridge_slash(text: str) -> str | None:
    head = text.strip().split(" ", 1)[0].lower()
    return head if head in _BRIDGE_SLASH_COMMANDS else None


_CLIENT_HANDLERS: dict[
    ClientEventType, Callable[..., Any]
] = {
    ClientEventType.HELLO: PiTerminalBridge._handle_hello,
    ClientEventType.PING: PiTerminalBridge._handle_ping,
    ClientEventType.SHUTDOWN: PiTerminalBridge._handle_shutdown,
    ClientEventType.SUBMIT: PiTerminalBridge._handle_submit,
    ClientEventType.RUN_CANCEL: PiTerminalBridge._handle_run_cancel,
    ClientEventType.SET_MODE: PiTerminalBridge._handle_set_mode,
    ClientEventType.CYCLE_MODE: PiTerminalBridge._handle_cycle_mode,
    ClientEventType.PERMISSION_RESPONSE: PiTerminalBridge._handle_permission_response,
}


def _start_stdin_line_reader(
    stream: TextIO,
    loop: asyncio.AbstractEventLoop,
) -> asyncio.Queue[str]:
    """Pump blocking stdin off the event loop (mirrors the naumi bridge)."""
    queue: asyncio.Queue[str] = asyncio.Queue()

    def pump() -> None:
        while True:
            line = stream.readline()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            except RuntimeError:
                return
            if line == "":
                return

    threading.Thread(target=pump, name="pi-ui-stdin", daemon=True).start()
    return queue


async def serve_stdio(bridge: PiTerminalBridge) -> None:
    """Serve JSONL from stdin to stdout until the UI disconnects."""
    bridge.bind_writer(sys.stdout)
    await bridge.emit_ready()
    loop = asyncio.get_running_loop()
    lines = _start_stdin_line_reader(sys.stdin, loop)
    while not bridge._closed:
        line = await lines.get()
        if line == "":
            await bridge.shutdown()
            return
        try:
            record = decode_jsonl_line(line)
            await bridge.handle_client_record(record)
        except Exception as exc:
            await bridge.emit_error(str(exc), code="bad_request")
