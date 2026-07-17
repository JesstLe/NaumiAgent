from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore


@pytest.mark.asyncio
async def test_harness_tools_are_read_only_and_share_one_service(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(tmp_path / "harness.db"),
    )

    tools = create_harness_tools(service)

    assert [tool.name for tool in tools] == [
        "harness_status",
        "harness_doctor",
        "harness_explain",
        "harness_replay",
        "harness_eval",
        "harness_read_knowledge",
        "harness_run_check",
    ]
    assert all(tool.metadata.read_only for tool in tools[:6])
    assert not tools[6].metadata.read_only
    assert all(tool.metadata.concurrency_safe for tool in tools)
    assert all(
        tool.parameters_schema == {"type": "object", "properties": {}}
        for tool in tools[:2]
    )
    assert "尚未配置" in await tools[0].execute()
    assert "诊断" in await tools[1].execute()
    assert "没有找到" in await tools[2].execute()
    assert "尚未配置" in await tools[4].execute()
    assert all(tool.name not in {"harness_trust", "harness_untrust"} for tool in tools)


@pytest.mark.asyncio
async def test_harness_check_tool_uses_service_and_validates_arguments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_run_check"
    )

    assert tool.metadata.concurrency_safe
    assert tool.parameters_schema["required"] == ["check_id", "run_id"]
    assert "参数无效" in await tool.execute(check_id=1, run_id="run-1")
    assert "尚未配置" in await tool.execute(check_id="unit", run_id="run-1")


@pytest.mark.asyncio
async def test_harness_explain_tool_validates_run_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(tmp_path / "harness.db"),
    )
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_explain"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {"run_id"}
    assert tool.parameters_schema["additionalProperties"] is False
    assert "参数无效" in await tool.execute(run_id=1)
    assert "没有找到" in await tool.execute(run_id="latest")


@pytest.mark.asyncio
async def test_harness_replay_tool_is_read_only_and_validates_run_id(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(tmp_path / "harness.db"),
    )
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_replay"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {"run_id"}
    assert tool.parameters_schema["additionalProperties"] is False
    assert "参数无效" in await tool.execute(run_id=1)
    assert "没有找到" in await tool.execute(run_id="latest")


@pytest.mark.asyncio
async def test_harness_eval_tool_is_read_only_allowlisted_and_validates_suite(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_eval"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {"suite"}
    assert tool.parameters_schema["additionalProperties"] is False
    assert "参数无效" in await tool.execute(suite=1)
    assert "参数无效" in await tool.execute(suite=" ")
    assert "参数无效" in await tool.execute(suite="x" * 1_025)
    assert "尚未配置" in await tool.execute()
