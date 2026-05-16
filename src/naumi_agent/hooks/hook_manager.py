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

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class HookPoint(StrEnum):
    """生命周期钩子点."""

    # Engine lifecycle
    ENGINE_RUN_START = "engine_run_start"
    ENGINE_RUN_END = "engine_run_end"

    # LLM calls
    LLM_CALL_START = "llm_call_start"
    LLM_CALL_END = "llm_call_end"

    # Tool execution
    TOOL_EXECUTE_START = "tool_execute_start"
    TOOL_EXECUTE_END = "tool_execute_end"

    # Agent (sub-agent) lifecycle
    AGENT_EXECUTE_START = "agent_execute_start"
    AGENT_EXECUTE_END = "agent_execute_end"

    # Task delegation
    DELEGATE_START = "delegate_start"
    DELEGATE_END = "delegate_end"

    # Message processing
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
    async def scope(self, point: HookPoint | str, callback: HookCallback):
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
            try:
                result = callback(ctx)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Hook %s for %s raised an error",
                    _func_name(callback),
                    ctx.point.value,
                )

        return ctx

    def fire_sync(self, ctx: HookContext) -> HookContext:
        """Fire hooks synchronously. Async callbacks are logged and skipped.

        Prefer `fire()` in async contexts.
        """
        callbacks = self._hooks.get(ctx.point.value, [])
        if not callbacks:
            return ctx

        for callback in callbacks:
            try:
                result = callback(ctx)
                if asyncio.iscoroutine(result):
                    logger.warning(
                        "Async hook %s called via fire_sync — skipped",
                        _func_name(callback),
                    )
            except Exception:
                logger.exception(
                    "Hook %s for %s raised an error",
                    _func_name(callback),
                    ctx.point.value,
                )

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


def _func_name(func: Any) -> str:
    if hasattr(func, "__qualname__"):
        return func.__qualname__
    if hasattr(func, "__name__"):
        return func.__name__
    return repr(func)
