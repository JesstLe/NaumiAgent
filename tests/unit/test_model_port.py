"""Model capability port contract and Engine injection tests."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig, ModelConfig
from naumi_agent.model.router import (
    ModelResponse,
    ModelRouter,
    StreamChunk,
    TokenUsage,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.ui.bridge import JsonlEngineBridge

MODEL_PORT_METHODS = {
    "call",
    "get_context_window",
    "get_cost_rates",
    "get_max_output",
    "get_model_capability_contract",
    "get_model_info",
    "get_reasoning_effort_status",
    "get_runtime_identity",
    "list_available_models",
    "reset_reasoning_effort",
    "resolve_model",
    "resolve_target",
    "set_reasoning_effort",
    "stream",
}


class _IncompleteModelPort:
    async def call(self) -> object:
        return object()


class _FalseyModelRouter(ModelRouter):
    def __bool__(self) -> bool:
        return False


class _DeterministicModelPort:
    """A non-ModelRouter implementation used for real Engine runs."""

    def __init__(self) -> None:
        self.delegate = ModelRouter(ModelConfig())
        self.calls: list[str] = []

    def get_model_info(self, model: str) -> dict[str, Any]:
        return self.delegate.get_model_info(model)

    def get_context_window(self, model: str) -> int:
        return self.delegate.get_context_window(model)

    def get_max_output(self, model: str) -> int:
        return self.delegate.get_max_output(model)

    def get_cost_rates(self, model: str) -> dict[str, float]:
        return self.delegate.get_cost_rates(model)

    def get_model_capability_contract(self, model: str | None = None):
        return self.delegate.get_model_capability_contract(model)

    def resolve_model(self, tier):
        return self.delegate.resolve_model(tier)

    def resolve_target(self, model: str):
        return self.delegate.resolve_target(model)

    def get_runtime_identity(self, model: str):
        return self.delegate.get_runtime_identity(model)

    async def list_available_models(
        self, provider_id: str | None = None, *, refresh: bool = False,
    ):
        return await self.delegate.list_available_models(provider_id, refresh=refresh)

    def get_reasoning_effort_status(self, model: str | None = None):
        return self.delegate.get_reasoning_effort_status(model)

    def set_reasoning_effort(self, value, *, model: str | None = None):
        return self.delegate.set_reasoning_effort(value, model=model)

    def reset_reasoning_effort(self, *, model: str | None = None):
        return self.delegate.reset_reasoning_effort(model=model)

    async def call(self, *args: Any, **kwargs: Any) -> ModelResponse:
        del args, kwargs
        self.calls.append("call")
        return ModelResponse(
            content="非流式 Port 完成",
            usage=TokenUsage(
                input_tokens=2,
                output_tokens=3,
                total_tokens=5,
                cost_usd=0.001,
            ),
            model="deterministic-model",
        )

    def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        del args, kwargs
        self.calls.append("stream")

        async def chunks() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(token="流式 Port 完成")
            yield StreamChunk(
                finish_reason="stop",
                usage=TokenUsage(
                    input_tokens=4,
                    output_tokens=5,
                    total_tokens=9,
                    cost_usd=0.002,
                ),
            )

        return chunks()


def _config(tmp_path: Path, *, catalog_path: str = "") -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        models=ModelConfig(catalog_path=catalog_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


def test_model_port_exposes_complete_public_capability_surface() -> None:
    methods = {
        name
        for name, value in vars(ModelPort).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert methods == MODEL_PORT_METHODS


def test_model_router_structurally_implements_model_port() -> None:
    assert isinstance(ModelRouter(ModelConfig()), ModelPort)


def test_incomplete_model_port_is_rejected() -> None:
    assert not isinstance(_IncompleteModelPort(), ModelPort)


@pytest.mark.asyncio
async def test_engine_uses_injected_model_port_everywhere(tmp_path: Path) -> None:
    from naumi_agent.tools import analysis as analysis_tools
    from naumi_agent.tools import pursuit as pursuit_tools

    port = ModelRouter(ModelConfig())
    with patch("naumi_agent.orchestrator.engine.load_provider_catalog") as load_catalog:
        engine = AgentEngine(
            _config(tmp_path, catalog_path="must-not-load.yaml"),
            model_port=port,
        )
    try:
        assert engine.router is port
        assert engine._router is port
        assert engine._compactor._router is port
        assert engine._planner._router is port
        assert engine._planner._classifier._router is port
        assert engine.subagent_manager._factory._router is port
        assert pursuit_tools._global_pursuit_loop is not None
        assert pursuit_tools._global_pursuit_loop._router is port
        assert analysis_tools._global_router is port
        assert engine.task_runner._planner._router is port
        load_catalog.assert_not_called()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_does_not_replace_explicit_falsey_model_port(
    tmp_path: Path,
) -> None:
    port = _FalseyModelRouter(ModelConfig())
    engine = AgentEngine(_config(tmp_path), model_port=port)
    try:
        assert engine.router is port
        assert engine.task_runner._planner._router is port
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_keeps_default_model_router(tmp_path: Path) -> None:
    engine = AgentEngine(_config(tmp_path))
    try:
        assert isinstance(engine.router, ModelRouter)
        assert isinstance(engine.router, ModelPort)
    finally:
        await engine.shutdown()


def test_engine_rejects_incomplete_model_port_before_runtime_io(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        TypeError,
        match="model_port 必须实现完整的 ModelPort 契约",
    ):
        AgentEngine(
            _config(tmp_path),
            model_port=_IncompleteModelPort(),  # type: ignore[arg-type]
        )
    assert not (tmp_path / ".naumi").exists()


@pytest.mark.asyncio
async def test_non_router_port_drives_real_run_and_streaming_receipt(
    tmp_path: Path,
) -> None:
    port = _DeterministicModelPort()
    assert isinstance(port, ModelPort)
    engine = AgentEngine(_config(tmp_path), model_port=port)
    events: list[tuple[str, dict[str, object]]] = []

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    try:
        direct = await engine.run("hi")
        streamed = await engine.run_streaming("直接回答这个问题", on_event)

        assert direct.status == "completed"
        assert direct.response == "非流式 Port 完成"
        assert streamed.status == "completed"
        assert streamed.response == "流式 Port 完成"
        assert streamed.receipt is not None
        assert [name for name in port.calls if name in {"call", "stream"}] == [
            "call",
            "stream",
        ]
        receipts = [data for event, data in events if event == "completion_receipt"]
        assert receipts == [streamed.receipt.to_dict()]
        assert engine._session is not None
        saved = await engine.session_store.load(engine._session.id)
        assert saved is not None
        assert any(
            message.get("content") == "流式 Port 完成"
            for message in saved.messages
        )
        assert saved.total_tokens == 14
        assert saved.total_cost_usd == pytest.approx(0.003)
        status = JsonlEngineBridge(engine, config_path="config.yaml").status_payload()
        assert status["model"] == port.resolve_model("capable")
        assert status["model_contract"]["max_context"] > 0
        changed = engine.router.set_reasoning_effort("auto")
        reset = engine.router.reset_reasoning_effort()
        assert changed.model == reset.model
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_planner_and_compactor_actually_invoke_non_router_port(
    tmp_path: Path,
) -> None:
    port = _DeterministicModelPort()
    engine = AgentEngine(_config(tmp_path), model_port=port)
    try:
        await engine._planner.plan(
            "请规划一个需要多个子系统协作、存在依赖和风险的复杂迁移项目"
        )
        planner_calls = port.calls.count("call")
        assert planner_calls >= 1

        messages = [{"role": "system", "content": "system"}]
        messages.extend(
            {"role": "user", "content": f"历史消息 {index} " * 20}
            for index in range(10)
        )
        compacted = await engine._compactor.compact(messages, max_tokens=10)

        assert port.calls.count("call") > planner_calls
        assert any("之前的对话摘要" in str(item.get("content")) for item in compacted)
    finally:
        await engine.shutdown()
