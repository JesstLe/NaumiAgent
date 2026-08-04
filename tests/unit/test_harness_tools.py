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
        "harness_eval_live",
        "harness_eval_live_batch",
        "harness_eval_replay",
        "harness_eval_baseline",
        "harness_eval_batch",
        "harness_eval_sandbox",
        "harness_eval_sandbox_retry",
        "harness_eval_sandbox_resume",
        "harness_eval_sandbox_retries",
        "harness_eval_sandbox_retry_detail",
        "harness_eval_sandbox_retry_retention_preview",
        "harness_eval_sandbox_retry_prune_authorize",
        "harness_eval_sandbox_retry_prune_execute",
        "harness_eval_baseline_promote",
        "harness_eval_compare",
        "harness_read_knowledge",
        "harness_run_check",
    ]
    assert all(tools[index].metadata.read_only for index in (0, 1, 2, 3, 4, 7, 8, 13, 14, 15, 20))
    assert not tools[5].metadata.read_only
    assert tools[5].metadata.requires_confirmation
    assert not tools[6].metadata.read_only
    assert tools[6].metadata.requires_confirmation
    assert not tools[9].metadata.read_only
    assert not tools[10].metadata.read_only
    assert not tools[11].metadata.read_only
    assert not tools[12].metadata.read_only
    assert not tools[16].metadata.read_only
    assert not tools[17].metadata.read_only
    assert not tools[18].metadata.read_only
    assert not tools[19].metadata.read_only
    assert not tools[21].metadata.read_only
    assert all(tool.metadata.concurrency_safe for tool in tools)
    assert all(tool.parameters_schema == {"type": "object", "properties": {}} for tool in tools[:2])
    assert "尚未配置" in await tools[0].execute()
    assert "诊断" in await tools[1].execute()
    assert "没有找到" in await tools[2].execute()
    assert "尚未配置" in await tools[4].execute()
    assert "评测错误" in await tools[7].execute(run_id="latest")
    assert "尚无 Baseline" in await tools[8].execute(suite="protocol")
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
    tool = next(item for item in create_harness_tools(service) if item.name == "harness_run_check")

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
    tool = next(item for item in create_harness_tools(service) if item.name == "harness_explain")

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
    tool = next(item for item in create_harness_tools(service) if item.name == "harness_replay")

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
    tool = next(item for item in create_harness_tools(service) if item.name == "harness_eval")

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {"suite"}
    assert tool.parameters_schema["additionalProperties"] is False
    assert "参数无效" in await tool.execute(suite=1)
    assert "参数无效" in await tool.execute(suite=" ")
    assert "参数无效" in await tool.execute(suite="x" * 1_025)
    assert "尚未配置" in await tool.execute()


@pytest.mark.asyncio
async def test_harness_sandbox_eval_tool_declares_delegation_and_validates_arguments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    tool = next(
        item for item in create_harness_tools(service) if item.name == "harness_eval_sandbox"
    )

    assert not tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert tool.metadata.delegated_tool_names == ("bash_run",)
    assert tool.parameters_schema["required"] == [
        "check_ids",
        "samples",
        "batch_id",
        "run_id",
    ]
    assert "参数无效" in await tool.execute(
        check_ids="unit",
        samples=5,
        batch_id="batch",
        run_id="run-1",
    )
    unavailable = await tool.execute(
        check_ids=["unit"],
        samples=5,
        batch_id="batch",
        run_id="run-1",
    )
    assert "sandbox_eval_service_unavailable" in unavailable


@pytest.mark.asyncio
async def test_harness_sandbox_retry_tool_requires_exact_durable_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    tool = next(
        item for item in create_harness_tools(service) if item.name == "harness_eval_sandbox_retry"
    )

    assert not tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert tool.metadata.delegated_tool_names == ("bash_run",)
    assert tool.parameters_schema["required"] == [
        "retry_action_id",
        "cancel_receipt_id",
        "cancel_receipt_sha256",
        "reason",
        "run_id",
    ]
    assert "参数无效" in await tool.execute(
        retry_action_id="",
        cancel_receipt_id=f"hsacr_{'a' * 24}",
        cancel_receipt_sha256="a" * 64,
        reason="恢复",
        run_id="run-1",
    )
    unavailable = await tool.execute(
        retry_action_id=f"hsar_{'b' * 24}",
        cancel_receipt_id=f"hsacr_{'a' * 24}",
        cancel_receipt_sha256="a" * 64,
        reason="恢复",
        run_id="run-1",
    )
    assert "sandbox_eval_service_unavailable" in unavailable


@pytest.mark.asyncio
async def test_harness_sandbox_resume_tool_binds_existing_dispatch_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    tool = next(
        item for item in create_harness_tools(service) if item.name == "harness_eval_sandbox_resume"
    )

    assert not tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert tool.metadata.delegated_tool_names == ("bash_run",)
    assert tool.parameters_schema["required"] == [
        "retry_action_id",
        "dispatch_id",
        "retry_receipt_id",
        "retry_receipt_sha256",
        "run_id",
    ]
    assert "参数无效" in await tool.execute(
        retry_action_id=f"hsar_{'a' * 24}",
        dispatch_id="",
        retry_receipt_id=f"hsarr_{'b' * 24}",
        retry_receipt_sha256="c" * 64,
        run_id="run-1",
    )
    unavailable = await tool.execute(
        retry_action_id=f"hsar_{'a' * 24}",
        dispatch_id=f"hsard_{'d' * 24}",
        retry_receipt_id=f"hsarr_{'b' * 24}",
        retry_receipt_sha256="c" * 64,
        run_id="run-1",
    )
    assert "sandbox_eval_service_unavailable" in unavailable


@pytest.mark.asyncio
async def test_harness_eval_replay_tool_is_read_only_and_validates_run_id(
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
        item for item in create_harness_tools(service) if item.name == "harness_eval_replay"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {"run_id"}
    assert tool.parameters_schema["additionalProperties"] is False
    assert "参数无效" in await tool.execute(run_id=1)
    assert "评测错误" in await tool.execute(run_id="latest")
