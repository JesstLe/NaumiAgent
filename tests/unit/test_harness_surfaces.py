from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine

PROFILE = """\
schema_version: 1
checks:
  - id: unit
    label: 单元测试
    argv: [uv, run, pytest, -q]
"""


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _engine(tmp_path: Path) -> AgentEngine:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(PROFILE, encoding="utf-8")
    config = AppConfig(
        workspace_root=str(workspace),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "data" / "sessions.db"),
            vector_db_path=str(tmp_path / "data" / "chroma"),
            long_term_enabled=False,
        ),
    )
    with patch.dict(
        "os.environ",
        {"NAUMI_STATE_HOME": str(tmp_path / "user-state")},
    ):
        return AgentEngine(config)


@pytest.mark.asyncio
async def test_engine_registers_only_read_only_harness_tools(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        status = engine.tool_registry.get("harness_status")
        doctor = engine.tool_registry.get("harness_doctor")
        assert status is not None and status.metadata.read_only
        assert doctor is not None and doctor.metadata.read_only
        assert engine.tool_registry.get("harness_trust") is None
        assert engine.tool_registry.get("harness_untrust") is None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_slash_flow_previews_confirms_and_revokes_trust(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        initial = _plain(await execute_slash_command(engine, "/harness status"))
        preview = _plain(await execute_slash_command(engine, "/harness trust"))
        still_untrusted = await engine.harness_service.status()
        confirmed = _plain(
            await execute_slash_command(engine, "/harness trust --confirm")
        )
        ready = _plain(await execute_slash_command(engine, "/harness status"))
        revoked = _plain(await execute_slash_command(engine, "/harness untrust"))

        assert "配置未受信任" in initial
        assert "仅预览" in preview
        assert "unit: uv run pytest -q" in preview
        assert not still_untrusted.trusted
        assert "已信任" in confirmed
        assert "Harness 已就绪" in ready
        assert "已撤销" in revoked
        assert not (await engine.harness_service.status()).trusted
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_slash_doctor_and_invalid_usage_are_actionable(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        doctor = _plain(await execute_slash_command(engine, "/harness doctor"))
        invalid = _plain(await execute_slash_command(engine, "/harness trust now"))
        unknown = _plain(await execute_slash_command(engine, "/harness unknown"))
        malformed = _plain(await execute_slash_command(engine, "/harness 'broken"))

        assert "Harness 诊断" in doctor
        assert "不会执行" in doctor
        assert "用法" in invalid
        assert "用法" in unknown
        assert "用法" in malformed
        assert "No closing quotation" not in malformed
    finally:
        await engine.shutdown()
