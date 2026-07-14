from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore


@pytest.mark.asyncio
async def test_harness_tools_are_read_only_and_share_one_service(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )

    tools = create_harness_tools(service)

    assert [tool.name for tool in tools] == [
        "harness_status",
        "harness_doctor",
        "harness_read_knowledge",
    ]
    assert all(tool.metadata.read_only for tool in tools)
    assert all(tool.metadata.concurrency_safe for tool in tools)
    assert all(
        tool.parameters_schema == {"type": "object", "properties": {}}
        for tool in tools[:2]
    )
    assert "尚未配置" in await tools[0].execute()
    assert "诊断" in await tools[1].execute()
    assert all(tool.name not in {"harness_trust", "harness_untrust"} for tool in tools)
