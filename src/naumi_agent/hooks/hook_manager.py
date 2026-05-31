"""Hook 管理器 — 类型安全的生命周期钩子系统.

Design inspired by:
- OpenAI Agents SDK: RunHooks (engine-scope) + AgentHooks (per-agent scope)
- CrewAI: typed event system with scoped handlers

Supports:
- Sync and async hook callbacks
- Engine-level hooks (all agents) via HookManager
- Scoped hooks (auto-removed on scope exit) via scope()
- Hook results can mutate context (e.g., inject extra tool args)
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class HookPoint(StrEnum):
    """生命周期钩子点."""

    # Engine lifecycle
    ENGINE_RUN_START = "engine_run_start"
    ENGINE_RUN_END = "engine_run_end"
    AGENT_STOP = "agent_stop"

    # LLM calls
    LLM_CALL_START = "llm_call_start"
    LLM_CALL_END = "llm_call_end"

    # Context assembly
    CONTEXT_ASSEMBLE_START = "context_assemble_start"
    CONTEXT_ASSEMBLE_END = "context_assemble_end"

    # Tool execution
    TOOL_PERMISSION_CHECK = "tool_permission_check"
    TOOL_EXECUTE_START = "tool_execute_start"
    TOOL_EXECUTE_END = "tool_execute_end"

    # Agent (sub-agent) lifecycle
    AGENT_EXECUTE_START = "agent_execute_start"
    AGENT_EXECUTE_END = "agent_execute_end"

    # Task delegation
    DELEGATE_START = "delegate_start"
    DELEGATE_END = "delegate_end"

    # Message processing
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    MESSAGE_IN = "message_in"
    MESSAGE_OUT = "message_out"


@dataclass
class HookContext:
    """传递给每个 hook 的上下文数据.

    Hooks may mutate `data` to influence downstream behavior.
    Setting `data["abort"] = True` cancels the operation (where applicable).
    """

    point: HookPoint
    data: dict[str, Any] = field(default_factory=dict)
    agent_name: str = ""
    session_id: str = ""
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def should_abort(self) -> bool:
        return bool(self.data.get("abort"))


HookCallback = Any


@dataclass(frozen=True)
class HookTraceEntry:
    """One hook callback execution trace entry."""

    point: str
    callback: str
    duration_ms: int
    aborted: bool = False
    error: str = ""
    sequence: int = 0


class HookManager:
    """Centralized hook registry with engine-level and scoped hooks.

    Usage::

        hooks = HookManager()

        @hooks.on(HookPoint.TOOL_EXECUTE_START)
        def log_tool(ctx: HookContext) -> None:
            print(f"Tool about to run: {ctx.data['tool_name']}")

        # Scoped hooks — auto-unregister on context manager exit
        async with hooks.scope(HookPoint.TOOL_EXECUTE_END, my_callback):
            ...  # callback active only within this block
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {}
        self._scoped: list[tuple[str, HookCallback]] = []
        self._trace: list[HookTraceEntry] = []
        self._max_trace_entries = 200
        self._trace_sequence = 0

    def on(self, point: HookPoint | str) -> Any:
        """Decorator to register a hook callback for a given point.

        Usage::

            @hooks.on(HookPoint.TOOL_EXECUTE_END)
            def on_tool_end(ctx: HookContext) -> None:
                ...
        """
        point_str = point if isinstance(point, str) else point.value

        def decorator(func: HookCallback) -> HookCallback:
            self.register(point_str, func)
            return func

        return decorator

    def register(self, point: HookPoint | str, callback: HookCallback) -> None:
        """Directly register a callback."""
        point_str = point if isinstance(point, str) else point.value
        self._hooks.setdefault(point_str, []).append(callback)
        logger.debug("Hook registered: %s → %s", point_str, _func_name(callback))

    def unregister(self, point: HookPoint | str, callback: HookCallback) -> None:
        """Remove a specific callback."""
        point_str = point if isinstance(point, str) else point.value
        callbacks = self._hooks.get(point_str, [])
        try:
            callbacks.remove(callback)
        except ValueError:
            pass

    @asynccontextmanager
    async def scope(
        self, point: HookPoint | str, callback: HookCallback
    ) -> AsyncIterator[None]:
        """Context manager: register a hook, auto-unregister on scope exit."""
        point_str = point if isinstance(point, str) else point.value
        self.register(point_str, callback)
        self._scoped.append((point_str, callback))
        try:
            yield
        finally:
            self.unregister(point_str, callback)
            try:
                self._scoped.remove((point_str, callback))
            except ValueError:
                pass

    async def fire(self, ctx: HookContext) -> HookContext:
        """Fire all hooks registered for the given point.

        Executes hooks in registration order. Sync callbacks are called directly,
        async callbacks are awaited. If any hook sets `abort=True`, subsequent
        hooks still run (all hooks get a chance) but `ctx.should_abort` becomes
        true.
        """
        callbacks = self._hooks.get(ctx.point.value, [])
        if not callbacks:
            return ctx

        for callback in callbacks:
            start = time.monotonic()
            error = ""
            try:
                result = callback(ctx)
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                logger.exception(
                    "Hook %s for %s raised an error",
                    _func_name(callback),
                    ctx.point.value,
                )
            finally:
                self._record_trace(HookTraceEntry(
                    point=ctx.point.value,
                    callback=_func_name(callback),
                    duration_ms=int((time.monotonic() - start) * 1000),
                    aborted=ctx.should_abort,
                    error=error,
                ))

        return ctx

    def fire_sync(self, ctx: HookContext) -> HookContext:
        """Fire hooks synchronously. Async callbacks are logged and skipped.

        Prefer `fire()` in async contexts.
        """
        callbacks = self._hooks.get(ctx.point.value, [])
        if not callbacks:
            return ctx

        for callback in callbacks:
            start = time.monotonic()
            error = ""
            try:
                if inspect.iscoroutinefunction(callback):
                    logger.warning(
                        "Async hook %s called via fire_sync — skipped",
                        _func_name(callback),
                    )
                    continue
                result = callback(ctx)
                if inspect.iscoroutine(result):
                    logger.warning(
                        "Async hook %s called via fire_sync — skipped",
                        _func_name(callback),
                    )
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                logger.exception(
                    "Hook %s for %s raised an error",
                    _func_name(callback),
                    ctx.point.value,
                )
            finally:
                self._record_trace(HookTraceEntry(
                    point=ctx.point.value,
                    callback=_func_name(callback),
                    duration_ms=int((time.monotonic() - start) * 1000),
                    aborted=ctx.should_abort,
                    error=error,
                ))

        return ctx

    def clear(self, point: HookPoint | str | None = None) -> None:
        """Remove all hooks. If point is given, only clear that point."""
        if point is None:
            self._hooks.clear()
            self._scoped.clear()
        else:
            point_str = point if isinstance(point, str) else point.value
            self._hooks.pop(point_str, None)
            self._scoped = [
                (p, cb) for p, cb in self._scoped if p != point_str
            ]

    def list_hooks(self) -> dict[str, list[str]]:
        """Return a summary of registered hooks (for debugging)."""
        return {
            point: [_func_name(cb) for cb in callbacks]
            for point, callbacks in self._hooks.items()
            if callbacks
        }

    def get_trace(self) -> list[HookTraceEntry]:
        """Return hook execution trace entries."""
        return list(self._trace)

    def clear_trace(self) -> None:
        """Clear hook execution trace entries."""
        self._trace.clear()

    def _record_trace(self, entry: HookTraceEntry) -> None:
        self._trace_sequence += 1
        self._trace.append(replace(entry, sequence=self._trace_sequence))
        if len(self._trace) > self._max_trace_entries:
            del self._trace[: len(self._trace) - self._max_trace_entries]


def _func_name(func: Any) -> str:
    qual: str | None = getattr(func, "__qualname__", None)
    if qual is not None:
        return qual
    name: str | None = getattr(func, "__name__", None)
    if name is not None:
        return name
    return repr(func)
