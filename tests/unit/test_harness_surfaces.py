from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.completion import (
    HarnessCompletionReceipt,
    HarnessReceiptCheck,
)
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.engine import AgentEngine

PROFILE = """\
schema_version: 1
knowledge:
  entrypoints: [AGENTS.md]
checks:
  - id: unit
    label: 单元测试
    argv: [python3, -c, "print('surface check ok')"]
evals:
  suites: [evals/protocol.yaml]
"""


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _engine(tmp_path: Path) -> AgentEngine:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(PROFILE, encoding="utf-8")
    (workspace / "AGENTS.md").write_text("HARNESS_SURFACE_RULE", encoding="utf-8")
    fixture = workspace / "evals" / "fixtures" / "hello.json"
    fixture.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "type": "hello",
            "version": 1,
            "payload": {"client": "legacy-surface"},
        },
        sort_keys=True,
    ).encode("utf-8")
    fixture.write_bytes(raw)
    (workspace / "evals" / "protocol.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "id": "surface-protocol",
                "title": "表面协议回归",
                "cases": [
                    {
                        "id": "legacy",
                        "runner": "protocol_hello",
                        "input": {"transport": "jsonl"},
                        "fixture": {
                            "path": "fixtures/hello.json",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        },
                        "expected": {
                            "outcome": "accepted",
                            "selected_version": 1,
                            "capabilities": [
                                "heartbeat",
                                "typed_ui_messages",
                                "workbench_snapshot",
                            ],
                        },
                        "metrics": {
                            "primary": "protocol_outcome_match",
                            "guardrails": ["no_model", "no_side_effect"],
                        },
                        "budget": {"max_duration_ms": 100},
                    }
                ],
                "budget": {"max_duration_ms": 5_000},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Tests"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"],
        cwd=workspace,
        check=True,
    )
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
async def test_engine_registers_harness_read_tools_and_trusted_check(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        status = engine.tool_registry.get("harness_status")
        doctor = engine.tool_registry.get("harness_doctor")
        explain = engine.tool_registry.get("harness_explain")
        replay = engine.tool_registry.get("harness_replay")
        eval_tool = engine.tool_registry.get("harness_eval")
        baseline_tool = engine.tool_registry.get("harness_eval_baseline")
        batch_tool = engine.tool_registry.get("harness_eval_batch")
        knowledge = engine.tool_registry.get("harness_read_knowledge")
        check = engine.tool_registry.get("harness_run_check")
        assert status is not None and status.metadata.read_only
        assert doctor is not None and doctor.metadata.read_only
        assert explain is not None and explain.metadata.read_only
        assert explain.metadata.concurrency_safe
        assert replay is not None and replay.metadata.read_only
        assert replay.metadata.concurrency_safe
        assert eval_tool is not None and eval_tool.metadata.read_only
        assert eval_tool.metadata.concurrency_safe
        assert baseline_tool is not None and baseline_tool.metadata.read_only
        assert baseline_tool.metadata.concurrency_safe
        assert batch_tool is not None and not batch_tool.metadata.read_only
        assert batch_tool.metadata.concurrency_safe
        assert knowledge is not None and knowledge.metadata.read_only
        assert knowledge.metadata.concurrency_safe
        assert check is not None and not check.metadata.read_only
        assert check.metadata.concurrency_safe
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
        knowledge = _plain(
            await execute_slash_command(engine, "/harness knowledge AGENTS.md")
        )
        check = _plain(await execute_slash_command(engine, "/harness check unit"))
        revoked = _plain(await execute_slash_command(engine, "/harness untrust"))
        eval_output = _plain(await execute_slash_command(engine, "/harness eval"))

        assert "配置未受信任" in initial
        assert "仅预览" in preview
        assert "unit: python3 -c" in preview
        assert not still_untrusted.trusted
        assert "已信任" in confirmed
        assert "Harness 已就绪" in ready
        assert "HARNESS_SURFACE_RULE" in knowledge
        assert "AGENTS.md" in knowledge
        assert "Harness 检查通过" in check
        assert "surface check ok" in check
        assert "已撤销" in revoked
        assert "Harness 离线 Eval" in eval_output
        assert "通过 1" in eval_output
        assert not (await engine.harness_service.status()).trusted
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_eval_slash_and_agent_tool_share_service_result(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        slash = _plain(
            await execute_slash_command(engine, "/harness eval surface-protocol")
        )
        tool = engine.tool_registry.get("harness_eval")
        assert tool is not None
        agent = await tool.execute(suite="surface-protocol")
        first = await engine.harness_service.eval_suites("surface-protocol")
        second = await engine.harness_service.eval_suites("surface-protocol")

        assert "表面协议回归" in slash
        assert "表面协议回归" in agent
        assert "实现回归 0" in slash
        assert "评测错误 0" in agent
        assert first.canonical_payload() == second.canonical_payload()
        identity = first.suites[0].baseline_identity
        assert identity is not None
        assert identity.model is None
        assert identity.baseline_eligible is False
        assert first.suites[0].baseline_identity_code == ""
        assert "Baseline" in slash
        assert "不可晋升" in slash
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_baseline_slash_and_agent_tool_share_empty_state(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        slash = _plain(
            await execute_slash_command(
                engine,
                "/harness baseline surface-protocol",
            )
        )
        tool = engine.tool_registry.get("harness_eval_baseline")
        assert tool is not None
        agent = await tool.execute(suite="surface-protocol")

        assert "尚无 Baseline" in slash
        assert "尚无 Baseline" in agent
        assert "稳定的重复 Eval cohort" in slash
        assert await tool.execute() == (
            "Harness Baseline 参数无效：suite 必须是字符串。"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_repeated_eval_persists_real_candidate_batch(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        slash = _plain(
            await execute_slash_command(
                engine,
                "/harness eval surface-protocol --repeat 5 --batch surface-batch-1",
            )
        )
        stored = await engine.harness_service.store.list_eval_results(
            engine.workspace_root,
            "surface-batch-1",
            "surface-protocol",
        )
        tool = engine.tool_registry.get("harness_eval_batch")
        assert tool is not None
        agent = await tool.execute(
            suite="surface-protocol",
            repetitions=5,
            batch_id="surface-batch-2",
        )
        tool_stored = await engine.harness_service.store.list_eval_results(
            engine.workspace_root,
            "surface-batch-2",
            "surface-protocol",
        )

        assert "完成 5/5 · 已保存 5" in slash
        assert "重复评测完成" in slash
        assert "surface-batch-1" in slash
        assert "完成 5/5 · 已保存 5" in agent
        assert len(stored) == len(tool_stored) == 5
        assert [item.sample_index for item in stored] == list(range(5))
        assert len({item.identity_sha256 for item in stored}) == 1
        assert all(item.result.baseline_identity is not None for item in stored)
        assert "5..100" in await tool.execute(
            suite="surface-protocol",
            repetitions=4,
        )
        assert "5..100" in await tool.execute(
            suite="surface-protocol",
            repetitions=101,
        )
        assert "batch_id 不能为空" in await tool.execute(
            suite="surface-protocol",
            batch_id="",
        )
        assert "batch_id 格式无效" in await tool.execute(
            suite="surface-protocol",
            batch_id="bad/id",
        )
        assert "未找到唯一匹配" in await tool.execute(
            suite="not-declared",
            batch_id="missing-suite",
        )
        assert await engine.harness_service.store.list_eval_results(
            engine.workspace_root,
            "missing-suite",
            "not-declared",
        ) == ()
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
        missing_knowledge = _plain(
            await execute_slash_command(engine, "/harness knowledge")
        )
        invalid_knowledge = _plain(
            await execute_slash_command(
                engine,
                "/harness knowledge AGENTS.md --unknown",
            )
        )

        assert "Harness 诊断" in doctor
        assert "不会执行" in doctor
        assert "用法" in invalid
        assert "用法" in unknown
        assert "用法" in malformed
        assert "用法" in missing_knowledge
        assert "用法" in invalid_knowledge
        assert "No closing quotation" not in malformed
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_harness_explain_slash_uses_real_durable_run(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = engine.harness_service.store
    assert store is not None
    contract = HarnessCompletionContract(
        run_id="slash-failed-run",
        session_id="slash-session",
        task_kind=HarnessTaskKind.CHANGE,
        objective="通过命令解释失败检查",
        required_checks=("unit",),
    )
    try:
        await store.start_run(
            workspace_root=engine.workspace_root,
            contract=contract,
            tree_fingerprint_before="a" * 64,
            started_at="2026-07-15T10:00:00+00:00",
        )
        check = HarnessCheckResult(
            check_id="unit",
            run_id=contract.run_id,
            status=HarnessCheckStatus.FAILED,
            tree_fingerprint="b" * 64,
            profile_digest="c" * 64,
            message="检查失败",
            output="raw output must not be rendered",
            exit_code=1,
            duration_ms=9,
        )
        await store.record_check(
            result=check,
            argv=("python3", "-m", "pytest", "tests/unit/test_small.py"),
            cwd=engine.workspace_root,
            started_at="2026-07-15T10:00:01+00:00",
            completed_at="2026-07-15T10:00:02+00:00",
        )
        await store.finish_run(
            run_id=contract.run_id,
            receipt=HarnessCompletionReceipt(
                run_id=contract.run_id,
                status="completed_unverified",
                task_kind=HarnessTaskKind.CHANGE,
                changed_files=("source.py",),
                checks=(
                    HarnessReceiptCheck(
                        id="unit",
                        status="failed",
                        tree_fingerprint="b" * 64,
                    ),
                ),
                criteria=(),
                warnings=("必需检查 unit 状态为 failed，不能作为通过证据。",),
                tree_fingerprint="b" * 64,
            ),
            completed_at="2026-07-15T10:00:03+00:00",
        )
        restored_service = HarnessService(
            workspace_root=engine.workspace_root,
            trust_store=HarnessTrustStore(tmp_path / "restored-trust.db"),
            store=HarnessStore(store.db_path),
        )
        engine.harness_service = restored_service
        for tool in create_harness_tools(restored_service):
            engine.tool_registry.register(tool)

        explained = _plain(
            await execute_slash_command(engine, "/harness explain latest")
        )
        replayed = _plain(
            await execute_slash_command(engine, "/harness replay latest")
        )
        detailed = _plain(
            await execute_slash_command(engine, "/harness detail latest")
        )
        explain_tool = engine.tool_registry.get("harness_explain")
        assert explain_tool is not None
        tool_explained = await explain_tool.execute(run_id=contract.run_id)
        replay_tool = engine.tool_registry.get("harness_replay")
        assert replay_tool is not None
        tool_replayed = await replay_tool.execute(run_id=contract.run_id)
        invalid = _plain(
            await execute_slash_command(engine, "/harness explain one two")
        )
        invalid_replay = _plain(
            await execute_slash_command(engine, "/harness replay one two")
        )
        invalid_detail = _plain(
            await execute_slash_command(engine, "/harness detail one two")
        )

        assert "slash-failed-run" in explained
        assert "verification_failure" in explained
        assert "重新运行" in explained
        assert "raw output must not be rendered" not in explained
        assert "slash-failed-run" in tool_explained
        assert "verification_failure" in tool_explained
        assert "raw output must not be rendered" not in tool_explained
        assert "Harness 安全回放" in replayed
        assert "已复现" in replayed
        assert "Harness 安全回放" in tool_replayed
        assert "raw output must not be rendered" not in tool_replayed
        assert "Harness 运行详情" in detailed
        assert "slash-failed-run" in detailed
        assert "失败分类" in detailed
        assert "验证失败" in detailed
        assert "Replay" in detailed
        assert "raw output must not be rendered" not in detailed
        assert "用法" in invalid
        assert "用法" in invalid_replay
        assert "用法" in invalid_detail
    finally:
        await engine.shutdown()
