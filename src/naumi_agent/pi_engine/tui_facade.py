"""Textual TUI engine facade for pi conversations.

The TUI embeds its conversation engine in-process and consumes the same
``run_streaming(content, sink)`` shape as the web routes (sink receives
RuntimeEvents).  On top of the web facade this class adds the small extra
surface the TUI touches: session helpers, status info, harmless lifecycle
no-ops, and a model-display shim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from naumi_agent.memory.session import Session
from naumi_agent.pi_engine.rpc import PiRpcError
from naumi_agent.pi_engine.web_facade import PiWebEngine
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType


class _PiRouterShim:
    """Minimal router surface so status/footer code keeps working."""

    def __init__(self, facade: PiTuiEngine) -> None:
        self._facade = facade

    def resolve_model(self, tier: str = "capable") -> str:
        model = self._facade._state_cache.get("model") or {}  # noqa: SLF001
        model_id = str(model.get("id") or "")
        provider = str(model.get("provider") or "")
        return f"pi/{provider}/{model_id}".rstrip("/") if model_id else "pi"

    def get_runtime_identity(self, model: str) -> SimpleNamespace:
        del model
        info = self._facade._state_cache.get("model") or {}  # noqa: SLF001
        return SimpleNamespace(
            provider=str(info.get("provider") or "pi"),
            api_format="pi-rpc",
            upstream_model=str(info.get("id") or ""),
        )

    def get_reasoning_effort_status(self, model: str | None = None):
        del model
        return SimpleNamespace(
            to_dict=lambda: {
                "model": "",
                "effective": "auto",
                "source": "auto",
                "supported": [],
                "default": None,
                "warning": "pi 引擎的思考强度由 pi 自身管理。",
            }
        )


class PiTuiEngine(PiWebEngine):
    """Drive Textual TUI conversations on one pi subprocess."""

    engine_kind = "pi"

    def __init__(self, config, session_store) -> None:
        super().__init__(config, session_store)
        self._config = config
        self._session: Session | None = None
        self._state_cache: dict[str, Any] = {}
        self._last_usage = {"tokens": 0, "cost": 0.0}
        self.router = _PiRouterShim(self)

    # -- TUI session surface ---------------------------------------------------

    @property
    def workspace_root(self) -> Path:
        return Path(self._config.resolve_workspace_root())

    async def get_or_create_session(self, title: str | None = None) -> Session:
        if self._session is not None:
            return self._session
        self._session = await self._session_store.create_session(
            title=title or "pi 会话",
            model="pi",
            engine="pi",
        )
        self._current_web_session = self._session.id
        return self._session

    async def load_session(self, session_id: str) -> bool:
        session = await self._session_store.load(session_id)
        if session is None:
            return False
        self._session = session
        self._current_web_session = session_id
        return True

    async def run_streaming(self, content: str, sink, turn_context: str = ""):
        """Run pi and emit the terminal event owned by the in-process TUI."""
        result = await super().run_streaming(content, sink, turn_context)
        await sink.emit(
            RuntimeEvent(
                id=uuid4().hex[:12],
                type=RuntimeEventType.RESPONSE_END,
                data={
                    "status": result.status,
                    "engine": "pi",
                    "turns": result.usage.turns,
                    "cost_usd": result.usage.total_cost_usd,
                },
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self._current_web_session,
            )
        )
        return result

    def reset(self) -> None:
        """New conversation: drop the active binding so the next run opens one."""
        self._session = None
        self._current_web_session = ""

    # -- status surfaces ----------------------------------------------------------

    async def refresh_state(self) -> dict[str, Any]:
        if self._rpc is None:
            return dict(self._state_cache)
        try:
            self._state_cache = await self._rpc.get_state()
        except PiRpcError:
            pass
        return dict(self._state_cache)

    def get_context_info(self) -> dict[str, Any]:
        model = self._state_cache.get("model") or {}
        window = int(model.get("contextWindow") or 0)
        used = int(
            self._state_cache.get("messageCount") or 0
        ) * 1000  # rough proxy, honest display
        percentage = round(used * 100 / window) if window else 0
        return {"used": used, "window": window, "percentage": percentage}

    def get_budget_info(self) -> dict[str, Any]:
        session = self._session
        used = float(getattr(session, "total_cost_usd", 0.0) or 0.0)
        return {"used_usd": used, "max_usd": None, "enabled": False}

    # -- lifecycle no-ops ------------------------------------------------------------

    async def start_long_running_services(self) -> None:
        return None

    def start_session_retention_worker(self) -> None:
        return None

    def stop_session_retention_worker(self) -> None:
        return None

    def wake_session_retention_worker(self) -> None:
        return None

    def session_retention_worker_snapshot(self) -> dict[str, Any]:
        return {"status": "not_applicable", "engine": "pi"}

    async def run_session_retention_once(self) -> dict[str, Any]:
        return {"status": "not_applicable", "engine": "pi"}

    async def preview_session_retention(self) -> dict[str, Any]:
        return {"candidates": [], "engine": "pi"}

    async def preview_session_delete(self, session_id: str) -> dict[str, Any]:
        del session_id
        return {"allowed": False, "reason": "pi 引擎不参与会话保留策略。"}

    def set_user_interaction_handler(self, handler: Any) -> None:
        del handler  # pi dialogs are answered headlessly by the facade

    def set_permission_confirmer(self, confirmer: Any) -> None:
        del confirmer

    # -- pi helpers for slash commands -------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        rpc = await self._ensure_rpc()
        return await rpc.get_available_models()

    async def set_pi_model(self, provider: str, model_id: str) -> dict[str, Any]:
        rpc = await self._ensure_rpc()
        result = await rpc.set_model(provider, model_id)
        await self.refresh_state()
        return result

    async def new_pi_conversation(self) -> None:
        rpc = await self._ensure_rpc()
        await rpc.new_session()
        await self.refresh_state()
        if self._current_web_session:
            state = dict(self._state_cache)
            self._pi_session_files[self._current_web_session] = str(
                state.get("sessionFile") or ""
            )
            self._pi_session_owner = self._current_web_session

    async def compact_conversation(self) -> None:
        rpc = await self._ensure_rpc()
        await rpc.compact()
