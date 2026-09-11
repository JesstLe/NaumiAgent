"""Unit tests for the pi terminal bridge against a fake RPC transport.

Drives the bridge through the same JSONL records the Node UI sends and
asserts the envelopes it writes back, including the permission round-trip.
"""

from __future__ import annotations

import asyncio
import io
import json

from naumi_agent.pi_engine.bridge import PiTerminalBridge
from naumi_agent.pi_engine.rpc import PiEventType, PiRpcError


class FakeRpc:
    """Duck-typed stand-in for PiRpcClient covering the bridge surface."""

    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.prompts: list[str] = []
        self.aborted = 0
        self.new_sessions = 0
        self.compacted = 0
        self.models: list[dict] = [
            {"id": "glm-4.7", "name": "GLM-4.7", "provider": "zai-coding-cn"},
            {"id": "glm-5.2", "name": "GLM-5.2", "provider": "zai-coding-cn"},
        ]
        self.ui_responses: list[tuple[str, dict]] = []
        self.fail_prompt = False

    async def prompt(self, message: str, *, images=None) -> None:
        if self.fail_prompt:
            raise PiRpcError("pi 命令 prompt 失败：连接失败")
        self.prompts.append(message)

    async def abort(self) -> None:
        self.aborted += 1

    async def new_session(self) -> None:
        self.new_sessions += 1

    async def compact(self, custom_instructions: str | None = None) -> dict:
        self.compacted += 1
        return {}

    async def get_state(self) -> dict:
        return {
            "sessionId": "pi-s-1",
            "model": {"id": "glm-4.7", "provider": "zai-coding-cn", "name": "GLM-4.7"},
        }

    async def set_model(self, provider: str, model_id: str) -> dict:
        if provider != "zai-coding-cn":
            raise PiRpcError(f"pi 命令 set_model 失败：Model not found: {provider}/{model_id}")
        return {"id": model_id, "provider": provider}

    async def get_available_models(self) -> list[dict]:
        return self.models

    async def set_session_name(self, name: str) -> None:
        return None

    async def send_extension_ui_response(
        self, request_id: str, fields: dict
    ) -> None:
        self.ui_responses.append((request_id, fields))

    async def stop(self) -> None:
        return None

    # -- helpers -------------------------------------------------------------

    def emit(self, event: dict) -> None:
        self.event_queue.put_nowait(event)

    def emit_simple_run(self, text: str = "好的") -> None:
        self.emit({"type": PiEventType.AGENT_START})
        self.emit(
            {
                "type": PiEventType.MESSAGE_UPDATE,
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": text,
                },
            }
        )
        self.emit({"type": PiEventType.AGENT_SETTLED})


class RecordingWriter(io.TextIOBase):
    """Non-blocking stdout stand-in that captures written JSONL lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.lock = asyncio.Lock()

    def write(self, text: str) -> int:
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines if line.strip()]

    async def wait_for(self, predicate, timeout: float = 5.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for record in self.records():
                if predicate(record):
                    return record
            await asyncio.sleep(0.01)
        raise AssertionError(
            "预期的事件未出现，当前输出：\n"
            + "\n".join(json.dumps(r, ensure_ascii=False) for r in self.records())
        )


HELLO = {
    "type": "hello",
    "payload": {
        "client": "unit-test",
        "minimum_version": 1,
        "maximum_version": 1,
        "capabilities": ["typed_ui_messages"],
    },
}


async def _started_bridge(rpc: FakeRpc) -> tuple[PiTerminalBridge, RecordingWriter]:
    writer = RecordingWriter()
    bridge = PiTerminalBridge()
    bridge.bind_writer(writer)
    await bridge.start(rpc=rpc)  # type: ignore[arg-type]
    await bridge.handle_client_record(HELLO)
    await writer.wait_for(lambda r: r["type"] == "ack")
    return bridge, writer


async def test_hello_negotiates_then_acks_with_status() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    records = writer.records()
    ack = next(r for r in records if r["type"] == "ack")
    assert ack["payload"]["negotiation"]["capabilities"] == ["typed_ui_messages"]
    status = next(
        r for r in records if r["type"] == "runtime/status"
    )
    assert status["payload"]["engine"] == "pi"
    assert status["payload"]["provider"] == "zai-coding-cn"
    assert status["payload"]["model"] == "glm-4.7"
    assert "/engine" in status["payload"]["slash_commands"]
    assert not any(r["type"] == "ready" for r in records), "ready 只能由 serve_stdio 发出一次"
    await bridge.shutdown()


async def test_submit_streams_messages_and_completes() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "你好"}, "request_id": "req-1"}
    )
    rpc.emit_simple_run("你好，我是 pi。")
    completed = await writer.wait_for(
        lambda r: r["type"] == "run/completed" and r.get("request_id") == "req-1"
    )
    assert completed["payload"]["status"] == "completed"
    assert completed["payload"]["response"] == "你好，我是 pi。"
    types = [r["type"] for r in writer.records()]
    assert "user/message" in types
    assert "run/started" in types
    ui_messages = [r for r in writer.records() if r["type"] == "ui/message"]
    assert any(
        m["payload"]["type"] == "assistant_stream" and m["payload"]["phase"] == "token"
        for m in ui_messages
    )
    assert rpc.prompts == ["你好"]
    await bridge.shutdown()


async def test_prompt_failure_completes_with_error() -> None:
    rpc = FakeRpc()
    rpc.fail_prompt = True
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "hi"}, "request_id": "req-2"}
    )
    completed = await writer.wait_for(
        lambda r: r["type"] == "run/completed" and r.get("request_id") == "req-2"
    )
    assert completed["payload"]["status"] == "error"
    assert "prompt" in completed["payload"]["error"]
    await bridge.shutdown()


async def test_busy_submit_is_rejected() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record({"type": "submit", "payload": {"text": "第一"}})
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "第二"}, "request_id": "req-busy"}
    )
    error = await writer.wait_for(
        lambda r: r["type"] == "error" and r.get("request_id") == "req-busy"
    )
    assert "正在执行" in error["payload"]["error"]
    rpc.emit_simple_run()
    await writer.wait_for(lambda r: r["type"] == "run/completed")
    await bridge.shutdown()


async def test_run_cancel_aborts_and_emits_cancelled() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "长任务"}, "request_id": "req-c"}
    )
    await asyncio.sleep(0.05)
    await bridge.handle_client_record(
        {"type": "run_cancel", "payload": {"reason": "太慢"}, "request_id": "cancel-1"}
    )
    rpc.emit_simple_run("部分输出")
    cancelled = await writer.wait_for(
        lambda r: r["type"] == "run/cancelled" and r.get("request_id") == "cancel-1"
    )
    assert cancelled["payload"]["reason"] == "太慢"
    assert rpc.aborted == 1
    await bridge.shutdown()


async def test_extension_confirm_round_trip() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record({"type": "submit", "payload": {"text": "任务"}})
    await asyncio.sleep(0.05)
    rpc.emit(
        {
            "type": PiEventType.EXTENSION_UI_REQUEST,
            "id": "pi-42",
            "method": "confirm",
            "title": "允许执行？",
            "message": "扩展请求运行危险命令",
        }
    )
    request = await writer.wait_for(lambda r: r["type"] == "permission/request")
    assert request["payload"]["tool_name"] == "pi 扩展确认"
    assert "危险命令" in json.dumps(request["payload"], ensure_ascii=False)
    await bridge.handle_client_record(
        {
            "type": "permission_response",
            "payload": {"request_id": request["payload"]["request_id"], "choice": "allow_once"},
        }
    )
    await writer.wait_for(lambda r: r["type"] == "permission/resolved")
    for _ in range(50):
        if rpc.ui_responses:
            break
        await asyncio.sleep(0.02)
    assert rpc.ui_responses == [("pi-42", {"confirmed": True})]
    rpc.emit_simple_run("完成")
    await writer.wait_for(lambda r: r["type"] == "run/completed")
    await bridge.shutdown()


async def test_extension_select_is_cancelled_honestly() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record({"type": "submit", "payload": {"text": "任务"}})
    await asyncio.sleep(0.05)
    rpc.emit(
        {
            "type": PiEventType.EXTENSION_UI_REQUEST,
            "id": "pi-77",
            "method": "select",
            "title": "选择",
            "options": ["a", "b"],
        }
    )
    notice = await writer.wait_for(
        lambda r: r["type"] == "ui/message"
        and r["payload"]["type"] == "system_notice"
        and "选择" in r["payload"]["content"]
    )
    assert notice["payload"]["level"] == "warning"
    for _ in range(50):
        if rpc.ui_responses:
            break
        await asyncio.sleep(0.02)
    assert rpc.ui_responses == [("pi-77", {"cancelled": True})]
    rpc.emit_simple_run()
    await writer.wait_for(lambda r: r["type"] == "run/completed")
    await bridge.shutdown()


async def test_slash_engine_and_models() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "/engine"}, "request_id": "r1"}
    )
    engine_notice = await writer.wait_for(
        lambda r: r["type"] == "ui/message"
        and r["payload"]["type"] == "system_notice"
        and "engine=pi" in r["payload"]["content"]
    )
    assert "naumi --engine naumi" in engine_notice["payload"]["content"]

    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "/models"}, "request_id": "r2"}
    )
    models_notice = await writer.wait_for(
        lambda r: r["type"] == "ui/message"
        and r["payload"]["type"] == "system_notice"
        and "glm-4.7" in r["payload"]["content"]
    )
    assert "zai-coding-cn" in models_notice["payload"]["content"]

    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "/model wrong/glm"}, "request_id": "r3"}
    )
    error = await writer.wait_for(
        lambda r: r["type"] == "error" and r.get("request_id") == "r3"
    )
    assert "Model not found" in error["payload"]["error"]
    await bridge.shutdown()


async def test_slash_new_and_compact() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record({"type": "submit", "payload": {"text": "/new"}})
    await writer.wait_for(
        lambda r: r["type"] == "ui/message"
        and r["payload"]["type"] == "system_notice"
        and "新会话" in r["payload"]["title"]
    )
    await bridge.handle_client_record({"type": "submit", "payload": {"text": "/compact"}})
    await writer.wait_for(
        lambda r: r["type"] == "ui/message"
        and r["payload"]["type"] == "system_notice"
        and "压缩" in r["payload"]["title"]
    )
    assert rpc.new_sessions == 1
    assert rpc.compacted == 1
    await bridge.shutdown()


async def test_unsupported_command_returns_chinese_error() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "task_panel", "payload": {}, "request_id": "r-tp"}
    )
    error = await writer.wait_for(
        lambda r: r["type"] == "error" and r.get("request_id") == "r-tp"
    )
    assert "不支持" in error["payload"]["error"]
    await bridge.shutdown()


async def test_set_mode_notice_and_echo() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "set_mode", "payload": {"mode": "plan"}}
    )
    await writer.wait_for(lambda r: r["type"] == "mode/changed")
    notices = [
        r
        for r in writer.records()
        if r["type"] == "ui/message" and r["payload"]["type"] == "system_notice"
    ]
    assert any("运行模式" in n["payload"]["title"] for n in notices)
    await bridge.shutdown()


async def test_ping_pong() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record({"type": "ping", "request_id": "p1"})
    pong = await writer.wait_for(lambda r: r["type"] == "pong")
    assert pong.get("request_id") == "p1"
    await bridge.shutdown()


async def test_secret_redaction_in_outputs() -> None:
    rpc = FakeRpc()
    bridge, writer = await _started_bridge(rpc)
    writer.lines.clear()
    await bridge.handle_client_record(
        {"type": "submit", "payload": {"text": "运行命令"}, "request_id": "r-sec"}
    )
    secret = "sk-abcdef1234567890abcdef1234567890abcdef12"
    rpc.emit({"type": PiEventType.AGENT_START})
    rpc.emit(
        {
            "type": PiEventType.MESSAGE_UPDATE,
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                      "delta": f"key={secret}"},
        }
    )
    rpc.emit({"type": PiEventType.AGENT_SETTLED})
    completed = await writer.wait_for(
        lambda r: r["type"] == "run/completed" and r.get("request_id") == "r-sec"
    )
    dumped = json.dumps(writer.records(), ensure_ascii=False)
    assert secret not in dumped
    assert "[REDACTED_API_KEY]" in completed["payload"]["response"]
    await bridge.shutdown()
