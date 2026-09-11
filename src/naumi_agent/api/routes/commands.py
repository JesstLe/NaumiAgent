"""Stream shared CLI command execution through the Web session transport."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from rich.text import Text

from naumi_agent.api.deps import AuthDep
from naumi_agent.api.routes.messages import (
    _active_chat_run_tasks,
    _engine_lock,
    _message_revision_index,
    _truncate_message_branch,
)
from naumi_agent.cli.slash_router import (
    _normalize_command,
    _split_command_batch,
    execute_slash_command,
)
from naumi_agent.streaming.events import EventType, StreamEvent
from naumi_agent.tools.base import ToolCall
from naumi_agent.ui.command_index import build_terminal_command_index

router = APIRouter(tags=["commands"])

# Terminal-only lifecycle and interactive terminal commands are not Web actions.
_SUPPORTED = frozenset(
    """
/help /history /tools /models /usage /version /pwd /diff /extensions /output
/goal /todo /tasks /task /glob /grep /read /file_read /write /file_write /edit /file_edit
/chaos /scale /state /vibe /eval /page /heal /dspy /graph /mcts /route
/speculate /jit /pointer /cooe /sleep /entropy /ooda /probe /hook /vision
/spar /world /fusion /consensus /pid /zkp /genesis /macro /cosmos /watchdog
/supervisor /autopsy /feedback /evolution
""".split()
)


class CommandRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(min_length=1, max_length=16000)
    runtime_mode: str = Field(default="default", pattern="^(default|plan|bypass)$")
    edit_message_id: str | None = None


def command_index():
    return [
        item
        for item in build_terminal_command_index("new_ui")
        if item.source == "shared_runtime" and item.command in _SUPPORTED
    ]


def validate_command(value: str) -> str:
    segments = _split_command_batch(value)
    if not segments or len(segments) > 1:
        raise HTTPException(422, "每次执行一条命令；多步操作请分别提交")
    normalized = _normalize_command(segments[0])
    token = normalized.split(maxsplit=1)[0]
    supported = {entry.command for entry in command_index()}
    aliases = {alias: entry.command for entry in command_index() for alias in entry.aliases}
    canonical = aliases.get(token, token)
    if canonical not in supported:
        raise HTTPException(422, f"Web 尚不支持命令 {token}，输入 / 查看可用命令")
    if canonical == "/goal" and normalized[len(token) :].strip().startswith(
        ("pursue", "interaction")
    ):
        raise HTTPException(422, "追踪执行与交互接管请使用 CLI 或 TUI；Web 可查看追踪证据")
    return canonical + normalized[len(token) :]


@router.get("/commands")
async def list_commands(auth: str = AuthDep):
    return {"commands": [entry.to_public_dict() for entry in command_index()]}


class _CommandEngine:
    """Forward to the same engine while injecting transport into tool execution."""

    def __init__(self, engine, callback):
        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "_callback", callback)
        object.__setattr__(self, "failed", False)

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def __setattr__(self, name, value):
        setattr(self._engine, name, value)

    async def execute_tool(self, tool_call, **kwargs):
        tool_call = ToolCall(
            id=f"web-command-{uuid.uuid4().hex}", name=tool_call.name, arguments=tool_call.arguments
        )
        kwargs["on_event"] = self._callback
        result = await self._engine.execute_tool(tool_call, **kwargs)
        if result.status != "success":
            object.__setattr__(self, "failed", True)
        return result


@router.post("/sessions/{session_id}/commands")
async def run_command(
    session_id: str,
    body: CommandRequest,
    request: Request,
    auth: str = AuthDep,
):
    command = validate_command(body.command)
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    if body.edit_message_id:
        _message_revision_index(session.messages, body.edit_message_id)
    if _engine_lock(request).locked():
        raise HTTPException(409, "Agent 正在执行，请等待完成后重试命令")
    return StreamingResponse(
        _command_stream(
            request,
            session_id,
            command,
            body.runtime_mode,
            edit_message_id=body.edit_message_id,
        ),
        media_type="text/event-stream",
    )


async def _command_stream(request, session_id, command, mode, *, edit_message_id=None):
    engine = request.app.state.engine
    store = request.app.state.chat_run_store
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
    run = None

    async def emit(kind, data):
        await queue.put(
            StreamEvent(type=kind, data=data, session_id=session_id, run_id=run.id if run else "")
        )

    async def on_tool(kind, data):
        if kind == "permission_bubble":
            fields = ("tool_name", "call_id", "reason", "status", "requires_confirmation")
            await emit(
                EventType.PERMISSION_REQUEST, {key: data[key] for key in fields if key in data}
            )
        elif kind in {"tool_start", "tool_end"}:
            await emit(
                EventType.TOOL_CALL_START if kind == "tool_start" else EventType.TOOL_CALL_END,
                {key: data[key] for key in ("name", "call_id", "content", "status") if key in data},
            )

    async def execute():
        nonlocal run
        previous_mode = None
        try:
            async with _engine_lock(request):
                if edit_message_id:
                    await _truncate_message_branch(
                        engine,
                        session_id,
                        edit_message_id,
                    )
                if not await engine.load_session(session_id):
                    raise ValueError("会话不存在")
                run = await store.start_run(session_id=session_id, user_message_id=uuid.uuid4().hex)
                _active_chat_run_tasks(request)[run.id] = (session_id, asyncio.current_task())
                await emit(EventType.AGENT_START, {"command": command.split(maxsplit=1)[0]})
                previous_mode = engine.runtime_mode
                engine.set_runtime_mode(mode)
                try:
                    adapter = _CommandEngine(engine, on_tool)
                    output = await execute_slash_command(adapter, command)
                finally:
                    engine.set_runtime_mode(previous_mode)
                output = Text.from_ansi(output).plain.strip() or "命令执行完成"
                session = await engine.session_store.load(session_id)
                if session is None:
                    raise ValueError("会话已不存在，命令结果无法保存")
                session.add_message("user", command)
                session.add_message("assistant", output)
                await engine.session_store.save(session)
                await emit(EventType.TOKEN_DELTA, {"token": output})
                status = "failed" if adapter.failed else "completed"
                await store.append_step(
                    run.id,
                    sequence=1,
                    stage="command",
                    status=status,
                    summary=command.split(maxsplit=1)[0],
                    detail=output,
                )
                await store.finish_run(run.id, status=status)
                await emit(EventType.AGENT_END, {"status": status})
        except asyncio.CancelledError:
            if run:
                await store.finish_run(run.id, status="cancelled")
            await emit(EventType.AGENT_END, {"status": "cancelled"})
            raise
        except Exception as exc:
            if run:
                await store.finish_run(run.id, status="failed")
            await emit(EventType.AGENT_ERROR, {"message": f"命令执行失败：{exc}"})
        finally:
            if run:
                _active_chat_run_tasks(request).pop(run.id, None)
            await queue.put(None)

    task = asyncio.create_task(execute())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                if event is None:
                    break
                yield event.to_sse()
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
