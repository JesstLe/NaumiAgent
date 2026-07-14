"""Permission policy port contract and Engine injection tests."""

from __future__ import annotations

import inspect
import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.safety.permissions import (
    PermissionAwareTool,
    PermissionChecker,
    PermissionDecision,
    PermissionMode,
)
from naumi_agent.tools.base import ToolCall


class _IncompletePermissionPort:
    mode = PermissionMode.MODERATE

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode

    def check(self, tool_name: str, args: object) -> object:
        del tool_name, args
        return object()


class _RecordingPermissionPort:
    def __init__(self, delegate: PermissionChecker) -> None:
        self.delegate = delegate
        self.calls: list[str] = []
        self.modes: list[PermissionMode] = []
        self.check_modes: list[PermissionMode] = []
        self.reset_count = 0

    @property
    def mode(self) -> PermissionMode:
        self.calls.append("mode")
        return self.delegate.mode

    def set_mode(self, mode: PermissionMode) -> None:
        self.calls.append("set_mode")
        self.modes.append(mode)
        self.delegate.set_mode(mode)

    def check(
        self,
        tool_name: str,
        args: Mapping[str, object],
        tool: PermissionAwareTool | None = None,
    ) -> PermissionDecision:
        self.calls.append("check")
        self.check_modes.append(self.delegate.mode)
        return self.delegate.check(tool_name, args, tool)

    def reset_counts(self) -> None:
        self.calls.append("reset_counts")
        self.reset_count += 1
        self.delegate.reset_counts()


class _FalseyPermissionPort(_RecordingPermissionPort):
    def __bool__(self) -> bool:
        return False


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


def test_permission_port_exposes_exact_policy_operations() -> None:
    methods = {
        name for name, value in vars(PermissionPort).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    properties = {
        name for name, value in vars(PermissionPort).items()
        if not name.startswith("_") and isinstance(value, property)
    }
    assert methods == {"set_mode", "check", "reset_counts"}
    assert properties == {"mode"}


def test_permission_checker_structurally_implements_port() -> None:
    assert isinstance(PermissionChecker(PermissionMode.MODERATE), PermissionPort)


def test_incomplete_permission_port_is_rejected() -> None:
    assert not isinstance(_IncompletePermissionPort(), PermissionPort)


def test_permission_checker_accepts_read_only_mapping_without_mutation() -> None:
    checker = PermissionChecker(PermissionMode.BYPASS)
    args = MappingProxyType({"path": "/outside"})

    decision = checker.check("file_read", args)

    assert decision.allowed
    assert dict(args) == {"path": "/outside"}


@pytest.mark.asyncio
async def test_engine_uses_injected_permission_port_and_legacy_alias(
    tmp_path: Path,
) -> None:
    port = _RecordingPermissionPort(
        PermissionChecker(PermissionMode.MODERATE, [str(tmp_path)], str(tmp_path))
    )
    engine = AgentEngine(_config(tmp_path), permission_port=port)

    try:
        assert engine._permission_checker is port
        assert engine.permission_mode is PermissionMode.MODERATE
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_does_not_replace_explicit_falsey_permission_port(
    tmp_path: Path,
) -> None:
    port = _FalseyPermissionPort(PermissionChecker(PermissionMode.STRICT))
    engine = AgentEngine(_config(tmp_path), permission_port=port)
    try:
        assert engine._permission_checker is port
        assert engine.permission_mode is PermissionMode.STRICT
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_keeps_default_permission_checker(tmp_path: Path) -> None:
    engine = AgentEngine(_config(tmp_path))
    try:
        assert isinstance(engine._permission_checker, PermissionChecker)
        assert isinstance(engine._permission_checker, PermissionPort)
    finally:
        await engine.shutdown()


def test_engine_rejects_incomplete_permission_port_before_runtime_io(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        TypeError,
        match="permission_port 必须实现完整的 PermissionPort 契约",
    ):
        AgentEngine(
            _config(tmp_path),
            permission_port=_IncompletePermissionPort(),  # type: ignore[arg-type]
        )
    assert not (tmp_path / ".naumi").exists()


@pytest.mark.asyncio
async def test_injected_port_preserves_real_plan_bypass_default_semantics(
    tmp_path: Path,
) -> None:
    readable = tmp_path / "readable.txt"
    readable.write_text("permission-port", encoding="utf-8")
    removable = tmp_path / "remove-me"
    removable.mkdir()
    (removable / "data.txt").write_text("x", encoding="utf-8")
    port = _RecordingPermissionPort(
        PermissionChecker(PermissionMode.MODERATE, [str(tmp_path)], str(tmp_path))
    )
    engine = AgentEngine(_config(tmp_path), permission_port=port)
    confirmations: list[dict[str, object]] = []

    async def confirm(payload: dict[str, object]) -> str:
        confirmations.append(payload)
        return "deny"

    engine.set_permission_confirmer(confirm)
    try:
        engine.set_runtime_mode(AgentRuntimeMode.PLAN)
        blocked = await engine._execute_tool(ToolCall(
            id="plan-write",
            name="file_write",
            arguments=json.dumps({"path": str(tmp_path / "blocked.txt"), "content": "no"}),
        ))
        allowed = await engine._execute_tool(ToolCall(
            id="plan-read",
            name="file_read",
            arguments=json.dumps({"path": str(readable)}),
        ))
        assert blocked.status == "error"
        assert not (tmp_path / "blocked.txt").exists()
        assert allowed.status == "success"
        assert port.check_modes[-1] is PermissionMode.STRICT

        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        removed = await engine._execute_tool(ToolCall(
            id="bypass-remove",
            name="bash_run",
            arguments=json.dumps({
                "command": f"rm -rf {shlex.quote(str(removable))} && echo removed",
            }),
        ))
        assert removed.status == "success"
        assert not removable.exists()
        assert confirmations == []
        assert engine.list_permission_grants() == ()
        assert port.check_modes[-1] is PermissionMode.BYPASS

        engine.set_runtime_mode(AgentRuntimeMode.DEFAULT)
        assert engine.permission_mode is PermissionMode.MODERATE
        engine.reset()
        assert port.reset_count == 1
        assert engine.permission_mode is PermissionMode.MODERATE
    finally:
        await engine.shutdown()

    assert port.modes == [
        PermissionMode.STRICT,
        PermissionMode.BYPASS,
        PermissionMode.MODERATE,
    ]
