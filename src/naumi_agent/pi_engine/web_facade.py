"""Web-facing engine facade driving pi through the FastAPI message paths.

``api.routes.messages._stream_response`` consumes a narrow engine surface
(``load_session`` / ``runtime_mode`` / ``set_runtime_mode`` /
``run_streaming``); this facade implements exactly that surface on top of a
single pi RPC subprocess, so pi sessions stream through the same SSE + chat
run pipeline as naumi sessions with zero route changes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from naumi_agent.pi_engine.env import resolve_env_refs
from naumi_agent.pi_engine.rpc import PiEventType, PiRpcClient, PiRpcError
from naumi_agent.pi_engine.web_events import PiWebEventTranslator
from naumi_agent.runtime.ports.events import RuntimeEvent


class PiWebEngineError(RuntimeError):
    """User-visible pi web engine failure (safe to serialize)."""


def _empty_usage() -> SimpleNamespace:
    return SimpleNamespace(
        turn=0,
        total_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_tokens=0,
        total_tokens=0,
    )


def _result(status: str, response: str, error: str, usage) -> SimpleNamespace:
    """Wrap a translator's usage into the result shape both UIs consume."""
    return SimpleNamespace(
        status=status,
        response=response,
        error=error,
        usage=SimpleNamespace(
            turns=usage.turn,
            total_cost_usd=usage.total_cost_usd,
            total_input_tokens=usage.input_tokens,
            total_output_tokens=usage.output_tokens,
            cache_tokens=usage.cache_tokens,
            total_tokens=usage.total_tokens,
        ),
    )


class PiWebEngine:
    """Serve naumi-session-shaped conversations on one pi subprocess."""

    def __init__(self, config, session_store) -> None:
        self._config = config
        self._session_store = session_store
        self._rpc: PiRpcClient | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._run_lock = asyncio.Lock()
        self._runtime_mode = "default"
        # NaumiAgent session id -> pi session file for continuation.
        self._pi_session_files: dict[str, str] = {}
        # Which web session currently owns the pi process's active session.
        self._pi_session_owner = ""
        # Last pi state snapshot for status display.
        self._state_cache: dict[str, Any] = {}
        self._current_web_session = ""
        self._closed = False

    # -- engine surface consumed by messages.py ------------------------------

    @property
    def runtime_mode(self) -> str:
        return self._runtime_mode

    def set_runtime_mode(self, mode: str) -> None:
        self._runtime_mode = str(mode or "default")

    async def load_session(self, session_id: str) -> bool:
        session = await self._session_store.load(session_id)
        if session is None:
            return False
        self._current_web_session = session_id
        return True

    async def run(self, content: str, turn_context: str = ""):
        sink_events: list[RuntimeEvent] = []

        async def collect(event: RuntimeEvent) -> None:
            sink_events.append(event)

        return await self.run_streaming(content, _CollectorSink(collect))

    async def run_streaming(
        self,
        content: str,
        sink,
        turn_context: str = "",
    ):
        if self._closed:
            raise PiWebEngineError("pi 引擎已关闭。")
        if self._run_lock.locked():
            raise PiWebEngineError("pi 引擎一次只能执行一个任务，请等待完成后再发送。")
        async with self._run_lock:
            rpc = await self._ensure_rpc()
            translator = PiWebEventTranslator(session_id=self._current_web_session)
            await self._prepare_pi_session(rpc)

            session = await self._session_store.load(self._current_web_session)
            if session is not None:
                session.add_message("user", content)
                await self._session_store.save(session)

            try:
                await rpc.prompt(content)
            except PiRpcError as exc:
                return _result("error", "", str(exc), _empty_usage())

            while True:
                event = await self._events.get()
                kind = str(event.get("type") or "")
                if kind == "__rpc_error__":
                    return _result(
                        "error",
                        "",
                        str(event.get("error") or "RPC 通道错误。"),
                        _empty_usage(),
                    )
                if kind == PiEventType.EXTENSION_UI_REQUEST:
                    await self._answer_extension_dialog(rpc, event)
                    continue
                for runtime_event in translator.feed(event):
                    await sink.emit(runtime_event)
                if kind == PiEventType.AGENT_SETTLED:
                    break

            response = translator.final_text()
            error = translator.error
            if session is not None:
                session.add_message("assistant", response or "")
                session.total_tokens += translator.total_tokens
                session.total_cost_usd += translator.total_cost_usd
                await self._session_store.save(session)
            status = "error" if error else "completed"
            return _result(status, response, error, translator)

    async def stop(self) -> None:
        self._closed = True
        if self._rpc is not None:
            await self._rpc.stop()
            self._rpc = None
        close_store = getattr(self._session_store, "close", None)
        if close_store is not None:
            try:
                await close_store()
            except Exception:
                pass  # store already closed or never opened

    async def shutdown(self) -> None:
        """Alias matching the AgentEngine surface consumers call on exit."""
        await self.stop()

    # -- internals -------------------------------------------------------------

    async def _ensure_rpc(self) -> PiRpcClient:
        if self._rpc is not None:
            return self._rpc
        from naumi_agent.pi_engine.extension import (
            default_pi_env,
            resolve_default_extension_args,
        )

        pi_config = self._config.engine.pi
        workspace_root = self._config.resolve_workspace_root()
        self._events = asyncio.Queue()
        self._rpc = await PiRpcClient.start(
            binary=pi_config.binary,
            provider=pi_config.provider,
            model=pi_config.model,
            extra_args=resolve_default_extension_args(
                workspace_root, list(pi_config.extra_args)
            ),
            cwd=str(workspace_root),
            event_handler=self._events.put_nowait,
            env=default_pi_env(resolve_env_refs(pi_config.env)),
        )
        return self._rpc

    async def _prepare_pi_session(self, rpc: PiRpcClient) -> None:
        """Bind this web session to its own pi conversation.

        The pi subprocess hosts one active conversation at a time, so track
        which web session currently owns it: reuse the recorded file, switch
        back to it, or open a fresh pi session when another web session owns
        the current one.
        """
        session_id = self._current_web_session
        state = await rpc.get_state()
        self._state_cache = dict(state)
        current_file = str(state.get("sessionFile") or "")
        wanted = self._pi_session_files.get(session_id)
        if wanted and wanted != current_file:
            await rpc.request("switch_session", sessionPath=wanted)
            self._pi_session_owner = session_id
            return
        if wanted:
            self._pi_session_owner = session_id
            return
        if current_file and self._pi_session_owner in ("", session_id):
            # A fresh pi process opened its own default session; claim it.
            self._pi_session_files[session_id] = current_file
            self._pi_session_owner = session_id
            return
        await rpc.new_session()
        state = await rpc.get_state()
        self._pi_session_files[session_id] = str(state.get("sessionFile") or "")
        self._pi_session_owner = session_id

    async def _answer_extension_dialog(
        self, rpc: PiRpcClient, event: dict[str, Any]
    ) -> None:
        """pi extension dialogs cannot block a headless web run; cancel them."""
        method = str(event.get("method") or "")
        request_id = str(event.get("id") or "")
        if method in {"notify", "setStatus", "setTitle", "setWidget"} or not request_id:
            return
        await rpc.send_extension_ui_response(request_id, {"cancelled": True})


class _CollectorSink:
    """Minimal sink adapter for the non-streaming ``run`` path."""

    def __init__(self, consumer) -> None:
        self._consumer = consumer

    async def emit(self, event: RuntimeEvent) -> None:
        await self._consumer(event)
