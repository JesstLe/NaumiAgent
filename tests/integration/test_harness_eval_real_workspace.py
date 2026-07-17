from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from naumi_agent.harness.eval_models import EvalRunStatus
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.trust import HarnessTrustStore


def _source_snapshot(repository: Path) -> dict[str, str]:
    paths = [
        repository / ".naumi" / "harness.yaml",
        repository / "src" / "naumi_agent" / "ui" / "protocol.py",
        *sorted((repository / "docs" / "harness" / "evals").rglob("*")),
    ]
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_real_workspace_protocol_eval_is_repeatable_and_read_only(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    before = _source_snapshot(repository)
    service = HarnessService(
        workspace_root=repository,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )

    first = await service.eval_suites("protocol-hello-core")
    second = await service.eval_suites("protocol-hello-core")
    after = _source_snapshot(repository)

    assert first.status is EvalRunStatus.PASSED
    assert len(first.suites) == 1
    assert first.suites[0].passed == 6
    assert first.suites[0].implementation_failures == 0
    assert first.suites[0].evaluation_errors == 0
    assert first.canonical_payload() == second.canonical_payload()
    assert before == after
