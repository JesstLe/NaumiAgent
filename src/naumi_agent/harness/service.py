"""Shared read-only Harness status, doctor, and user-only trust facade."""

from __future__ import annotations

import asyncio
import re
import shlex
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from naumi_agent.harness.checks import (
    HarnessCheckResult,
    HarnessCheckRunner,
    HarnessCheckStatus,
    select_required_check_ids,
    validate_run_id,
)
from naumi_agent.harness.completion import (
    CompletionGate,
    CompletionGateInput,
    CompletionGateResult,
    HarnessCompletionReceipt,
    HarnessEvidenceRef,
    HarnessRunState,
    build_completion_contract,
    render_completion_contract_context,
)
from naumi_agent.harness.context import (
    HarnessKnowledgeContextComposer,
    KnowledgeContextBundle,
    safe_markdown_fence,
)
from naumi_agent.harness.eval import (
    HarnessEvalAssetError,
    evaluate_declared_suites,
    evaluate_suite_repetitions,
    resolve_declared_eval_suite,
)
from naumi_agent.harness.eval_models import EvalRunStatus, HarnessEvalReport
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.eval_surface import (
    HarnessEvalBaselineStatus,
    HarnessEvalBatchStatus,
    HarnessEvalComparisonRunStatus,
    HarnessEvalPromotionStatus,
    build_eval_baseline_status,
)
from naumi_agent.harness.evidence import EvidenceCollector
from naumi_agent.harness.explain import (
    HarnessExplainer,
    HarnessExplainLookup,
)
from naumi_agent.harness.fingerprint import (
    TreeFingerprint,
    TreeFingerprintError,
    changed_paths_between,
    compute_tree_fingerprint,
)
from naumi_agent.harness.knowledge import (
    KnowledgeIndexSnapshot,
    KnowledgeReadResult,
    RepositoryKnowledgeIndex,
)
from naumi_agent.harness.models import (
    HarnessProfile,
    HarnessProfileSnapshot,
    HarnessProfileStatus,
    HarnessTaskKind,
)
from naumi_agent.harness.profile import load_harness_profile
from naumi_agent.harness.replay import capture_replay_baseline, replay_stored_run
from naumi_agent.harness.replay_models import HarnessReplayLookup
from naumi_agent.harness.store import (
    HarnessSessionDeleteImpact,
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoredEvalResult,
    HarnessStoreError,
)
from naumi_agent.harness.trust import (
    HarnessTrustRecord,
    HarnessTrustStore,
    HarnessTrustStoreError,
)
from naumi_agent.safety.guardrails import OutputGuardrail

_STORE_WARNING = (
    "infrastructure_error: Harness 状态库写入失败，本次主任务结果仍会返回；"
    "请检查用户状态目录权限。"
)


class HarnessStatusCode(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


class HarnessKnowledgeStatusCode(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    UNTRUSTED = "untrusted"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class HarnessStatus:
    code: HarnessStatusCode
    snapshot: HarnessProfileSnapshot
    trusted: bool
    stored_trust: HarnessTrustRecord | None = None
    trust_store_available: bool = True

    @property
    def profile_digest(self) -> str | None:
        return self.snapshot.digest


@dataclass(frozen=True)
class HarnessDoctorFinding:
    code: str
    level: Literal["ok", "info", "warning", "error"]
    message: str
    hint: str = ""


@dataclass(frozen=True)
class HarnessDoctorReport:
    status: HarnessStatus
    findings: tuple[HarnessDoctorFinding, ...]
    command_summaries: tuple[str, ...]


@dataclass(frozen=True)
class HarnessKnowledgeContextResult:
    code: HarnessKnowledgeStatusCode
    bundle: KnowledgeContextBundle | None
    message: str
    cache_hit: bool = False
    selection_cache_hit: bool = False
    index_fingerprint: str | None = None
    elapsed_ms: int = 0


class HarnessService:
    """Facade shared by manual commands and read-only Agent tools."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        trust_store: HarnessTrustStore,
        profile_path: str | Path | None = None,
        store: HarnessStore | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._trust_store = trust_store
        self._store = store
        self._evidence_collector = (
            EvidenceCollector(store=store) if store is not None else None
        )
        self._profile_path = profile_path
        self._knowledge_index = RepositoryKnowledgeIndex(self.workspace_root)
        self._check_runner = HarnessCheckRunner(workspace_root=self.workspace_root)
        self._completion_gate = CompletionGate()
        self._explainer = HarnessExplainer()
        self._check_results: OrderedDict[
            str,
            OrderedDict[str, HarnessCheckResult],
        ] = OrderedDict()
        self._check_results_lock = asyncio.Lock()
        self._max_check_runs = 128
        self._store_state_lock = asyncio.Lock()
        self._persisted_run_ids: OrderedDict[str, None] = OrderedDict()
        self._store_warnings: OrderedDict[str, list[str]] = OrderedDict()
        self._max_persisted_runs = 128
        self._knowledge_composer = HarnessKnowledgeContextComposer(
            self._knowledge_index
        )
        self._knowledge_cache: KnowledgeIndexSnapshot | None = None
        self._knowledge_lock = asyncio.Lock()
        self._knowledge_build: tuple[
            str,
            asyncio.Task[KnowledgeIndexSnapshot],
        ] | None = None
        self._selection_cache: OrderedDict[
            tuple[str, str, int | None],
            KnowledgeContextBundle,
        ] = OrderedDict()
        self._last_git_audit_at = 0.0
        self._git_audit_interval_seconds = 30.0

    @property
    def store(self) -> HarnessStore | None:
        return self._store

    async def preview_session_delete(
        self,
        session_id: str,
        *,
        workspace_root: str | Path | None = None,
    ) -> HarnessSessionDeleteImpact:
        """Preview records associated with one exact workspace/session boundary."""
        workspace = Path(workspace_root or self.workspace_root).expanduser().resolve()
        if self._store is None:
            return HarnessSessionDeleteImpact(
                workspace_root=str(workspace),
                session_id=session_id,
            )
        return await self._store.preview_session_delete(workspace, session_id)

    async def eval_suites(self, target: str | None = None) -> HarnessEvalReport:
        """Run declared static Eval Suites without trust, commands, models, or writes."""
        if target is not None and not isinstance(target, str):
            raise ValueError("suite 必须是字符串。")
        normalized_target = target.strip() if target is not None else None
        if normalized_target == "":
            raise ValueError("suite 不能为空。")
        if normalized_target is not None and len(normalized_target) > 1_024:
            raise ValueError("suite 不能超过 1024 个字符。")
        status = await self.status()
        if status.code is HarnessStatusCode.MISSING:
            return HarnessEvalReport(
                requested=normalized_target or "all",
                status=EvalRunStatus.EVALUATION_ERROR,
                code="profile_missing",
                message="当前工作区尚未配置 Harness Profile，无法运行离线 Eval。",
            )
        if status.code is HarnessStatusCode.INVALID:
            return HarnessEvalReport(
                requested=normalized_target or "all",
                status=EvalRunStatus.EVALUATION_ERROR,
                code="profile_invalid",
                message="Harness Profile 无效；请先运行 /harness doctor 修复后再运行 Eval。",
            )
        profile = status.snapshot.profile
        assert profile is not None
        return await asyncio.to_thread(
            evaluate_declared_suites,
            self.workspace_root,
            profile.evals.suites,
            normalized_target,
            profile_digest=status.profile_digest,
            profile_trusted=status.trusted,
        )

    async def eval_baseline_status(
        self,
        suite_id: str,
    ) -> HarnessEvalBaselineStatus:
        """Read active Baseline and recent immutable comparisons for one Suite."""
        if not isinstance(suite_id, str):
            raise ValueError("suite_id 必须是字符串。")
        suite = suite_id.strip()
        if not suite or len(suite) > 64:
            raise ValueError("suite_id 必须是 1..64 个字符。")
        if self._store is None:
            return HarnessEvalBaselineStatus(
                status="unavailable",
                suite_id=suite,
                message="Harness 状态库尚未初始化。",
            )
        try:
            active = await self._store.get_active_eval_baseline(
                self.workspace_root,
                suite,
            )
            comparisons = (
                await self._store.list_eval_comparison_receipts(
                    self.workspace_root,
                    suite,
                    baseline_id=active.id,
                    limit=20,
                )
                if active is not None
                else ()
            )
        except HarnessStoreError:
            return HarnessEvalBaselineStatus(
                status="unavailable",
                suite_id=suite,
                message="Harness Eval 状态库损坏、不可读或正忙。",
            )
        return build_eval_baseline_status(suite, active, comparisons)

    async def eval_repetition_batch(
        self,
        suite: str,
        *,
        repetitions: int = 5,
        batch_id: str | None = None,
    ) -> HarnessEvalBatchStatus:
        """Run and durably append one repeated static Eval candidate cohort."""
        if not isinstance(suite, str) or not suite.strip() or len(suite.strip()) > 1_024:
            raise ValueError("suite 必须是 1..1024 个字符。")
        if not isinstance(repetitions, int) or isinstance(repetitions, bool):
            raise ValueError("repetitions 必须是整数。")
        if not 5 <= repetitions <= 100:
            raise ValueError("repetitions 必须在 5..100 之间。")
        normalized_batch = batch_id.strip() if isinstance(batch_id, str) else ""
        if batch_id is not None and not normalized_batch:
            raise ValueError("batch_id 不能为空。")
        if normalized_batch and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            normalized_batch,
        ):
            raise ValueError("batch_id 格式无效。")
        if not normalized_batch:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            normalized_batch = f"eval-{timestamp}-{uuid.uuid4().hex[:8]}"
        if self._store is None:
            return _eval_batch_error(
                normalized_batch,
                suite.strip(),
                repetitions,
                "store_unavailable",
                "Harness 状态库尚未初始化，未运行重复 Eval。",
            )
        status = await self.status()
        if status.code is HarnessStatusCode.MISSING:
            return _eval_batch_error(
                normalized_batch,
                suite.strip(),
                repetitions,
                "profile_missing",
                "当前工作区尚未配置 Harness Profile。",
            )
        if status.code is HarnessStatusCode.INVALID:
            return _eval_batch_error(
                normalized_batch,
                suite.strip(),
                repetitions,
                "profile_invalid",
                "Harness Profile 无效；请先运行 /harness doctor。",
            )
        profile = status.snapshot.profile
        assert profile is not None and status.profile_digest is not None
        try:
            suite_path = resolve_declared_eval_suite(
                self.workspace_root,
                profile.evals.suites,
                suite.strip(),
            )
        except HarnessEvalAssetError as exc:
            return _eval_batch_error(
                normalized_batch,
                suite.strip(),
                repetitions,
                exc.code,
                str(exc),
            )
        batch = await asyncio.to_thread(
            evaluate_suite_repetitions,
            self.workspace_root,
            suite_path,
            repetitions=repetitions,
            profile_digest=status.profile_digest,
            profile_trusted=status.trusted,
        )
        persisted = 0
        created_at = datetime.now(UTC).isoformat()
        try:
            for index, result in enumerate(batch.results):
                await self._store.record_eval_result(
                    workspace_root=self.workspace_root,
                    batch_id=normalized_batch,
                    sample_index=index,
                    result=result,
                    created_at=created_at,
                )
                persisted += 1
        except (HarnessStoreError, ValueError) as exc:
            return _eval_batch_error(
                normalized_batch,
                batch.results[0].suite_id if batch.results else suite.strip(),
                repetitions,
                "batch_persistence_failed",
                str(exc),
                completed=batch.completed,
                persisted=persisted,
                duration_ms=batch.duration_ms,
            )
        identity = (
            batch.results[0].baseline_identity if batch.results else None
        )
        return HarnessEvalBatchStatus(
            status=batch.status,
            code=batch.code,
            batch_id=normalized_batch,
            suite_id=batch.results[0].suite_id if batch.results else suite.strip(),
            requested=batch.requested,
            completed=batch.completed,
            persisted=persisted,
            passed_cases=sum(result.passed for result in batch.results),
            implementation_failures=sum(
                result.implementation_failures for result in batch.results
            ),
            evaluation_errors=sum(
                result.evaluation_errors for result in batch.results
            ),
            skipped=sum(result.skipped for result in batch.results),
            duration_ms=batch.duration_ms,
            baseline_eligible=bool(identity and identity.baseline_eligible),
            identity_sha256=identity.identity_sha256 if identity is not None else "",
        )

    async def promote_eval_baseline(
        self,
        suite_id: str,
        batch_id: str,
        *,
        actor: Literal["user", "agent"],
        reason: str,
    ) -> HarnessEvalPromotionStatus:
        """Explicitly promote one eligible stored cohort through the H5b gate."""
        if not isinstance(suite_id, str) or not suite_id.strip():
            raise ValueError("suite_id 不能为空。")
        suite = suite_id.strip()
        if len(suite) > 64:
            raise ValueError("suite_id 不能超过 64 个字符。")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id 不能为空。")
        batch = batch_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch):
            raise ValueError("batch_id 格式无效。")
        if actor not in {"user", "agent"}:
            raise ValueError("actor 必须是 user 或 agent。")
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 2_000:
            raise ValueError("reason 必须是 3..2000 个字符。")
        normalized_reason = reason.strip()
        if self._store is None:
            return _eval_promotion_error(
                suite,
                batch,
                "store_unavailable",
                "Harness 状态库尚未初始化。",
            )
        try:
            samples = await self._store.list_eval_results(
                self.workspace_root,
                batch,
                suite,
                limit=10_000,
            )
            if not samples:
                return _eval_promotion_error(
                    suite,
                    batch,
                    "cohort_missing",
                    "未找到该 workspace/suite/batch 的 Eval samples。",
                )
            existing = await self._store.get_eval_baseline_by_batch(
                self.workspace_root,
                suite,
                batch,
            )
            baseline = await self._store.promote_eval_baseline(
                workspace_root=self.workspace_root,
                batch_id=batch,
                suite_id=suite,
                promoted_by=actor,
                promotion_reason=normalized_reason,
                created_at=datetime.now(UTC).isoformat(),
            )
            active = await self._store.get_active_eval_baseline(
                self.workspace_root,
                suite,
            )
            event = await self._store.get_eval_baseline_event(
                self.workspace_root,
                suite,
                baseline.id,
            )
        except ValueError as exc:
            return _eval_promotion_error(
                suite,
                batch,
                "eligibility_rejected",
                str(exc),
            )
        except HarnessStoreError:
            return _eval_promotion_error(
                suite,
                batch,
                "store_error",
                "Harness Eval 状态库损坏、不可读或正忙。",
            )
        if active is None or event is None:
            return _eval_promotion_error(
                suite,
                batch,
                "selector_missing",
                "晋升后 selector 或审计事件缺失；状态库可能损坏。",
            )
        if active.id != baseline.id:
            promotion_status = "not_selected"
        elif existing is not None:
            promotion_status = "already_active"
        else:
            promotion_status = "promoted"
        return HarnessEvalPromotionStatus(
            status=promotion_status,
            suite_id=suite,
            batch_id=batch,
            baseline_id=baseline.id,
            active_baseline_id=active.id,
            previous_baseline_id=event.previous_baseline_id,
            version=baseline.version,
            sample_count=baseline.sample_count,
            promoted_by=baseline.promoted_by,
            promotion_reason=baseline.promotion_reason,
            created_at=baseline.created_at,
        )

    async def compare_eval_candidate(
        self,
        suite_id: str,
        candidate_batch_id: str,
    ) -> HarnessEvalComparisonRunStatus:
        """Compare one complete candidate against the active immutable Baseline."""
        if not isinstance(suite_id, str) or not suite_id.strip():
            raise ValueError("suite_id 不能为空。")
        suite = suite_id.strip()
        if len(suite) > 64:
            raise ValueError("suite_id 不能超过 64 个字符。")
        if not isinstance(candidate_batch_id, str) or not candidate_batch_id.strip():
            raise ValueError("candidate_batch_id 不能为空。")
        candidate_batch = candidate_batch_id.strip()
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            candidate_batch,
        ):
            raise ValueError("candidate_batch_id 格式无效。")
        if self._store is None:
            return _eval_comparison_error(
                suite,
                candidate_batch,
                "store_unavailable",
                "Harness 状态库尚未初始化。",
            )
        try:
            baseline = await self._store.get_active_eval_baseline(
                self.workspace_root,
                suite,
            )
            if baseline is None:
                return _eval_comparison_error(
                    suite,
                    candidate_batch,
                    "baseline_missing",
                    "当前 Suite 尚无 active Baseline。",
                )
            if candidate_batch == baseline.batch_id:
                return _eval_comparison_error(
                    suite,
                    candidate_batch,
                    "candidate_is_baseline",
                    "Candidate batch 不能与 active Baseline batch 相同。",
                )
            existing = await self._store.get_eval_comparison_receipt(
                self.workspace_root,
                suite,
                baseline.id,
                candidate_batch,
            )
            if existing is not None:
                active = await self._store.get_active_eval_baseline(
                    self.workspace_root,
                    suite,
                )
                return _eval_comparison_status(
                    existing,
                    baseline.version,
                    status=(
                        "existing"
                        if active is not None and active.id == baseline.id
                        else "stale_baseline"
                    ),
                )
            baseline_records = await self._store.list_eval_results(
                self.workspace_root,
                baseline.batch_id,
                suite,
                limit=10_000,
            )
            candidate_records = await self._store.list_eval_results(
                self.workspace_root,
                candidate_batch,
                suite,
                limit=10_000,
            )
            if not candidate_records:
                return _eval_comparison_error(
                    suite,
                    candidate_batch,
                    "candidate_missing",
                    "未找到该 workspace/suite/batch 的 Candidate samples。",
                )
            if not _eval_candidate_complete(candidate_records):
                return _eval_comparison_error(
                    suite,
                    candidate_batch,
                    "candidate_incomplete",
                    "Candidate sample 不连续、Identity 不统一或 repetitions 未完成。",
                )
            receipt = build_eval_comparison_receipt(
                workspace_root=self.workspace_root,
                suite_id=suite,
                baseline_id=baseline.id,
                baseline_batch_id=baseline.batch_id,
                baseline_samples_sha256=baseline.samples_sha256,
                baseline_samples=tuple(
                    EvalReceiptSample(
                        sample_index=item.sample_index,
                        result_sha256=item.result_sha256,
                        result=item.result,
                    )
                    for item in baseline_records
                ),
                current_batch_id=candidate_batch,
                current_samples=tuple(
                    EvalReceiptSample(
                        sample_index=item.sample_index,
                        result_sha256=item.result_sha256,
                        result=item.result,
                    )
                    for item in candidate_records
                ),
                created_at=datetime.now(UTC).isoformat(),
            )
            stored = await self._store.record_eval_comparison_receipt(receipt)
            active = await self._store.get_active_eval_baseline(
                self.workspace_root,
                suite,
            )
        except ValueError as exc:
            return _eval_comparison_error(
                suite,
                candidate_batch,
                "comparison_rejected",
                str(exc),
            )
        except HarnessStoreError:
            return _eval_comparison_error(
                suite,
                candidate_batch,
                "store_error",
                "Harness Eval 状态库损坏、不可读或正忙。",
            )
        return _eval_comparison_status(
            stored,
            baseline.version,
            status=(
                "created"
                if active is not None and active.id == baseline.id
                else "stale_baseline"
            ),
        )

    async def run_check(self, *, check_id: str, run_id: str) -> HarnessCheckResult:
        """Run one exact check from the currently trusted Profile."""
        normalized_check_id = check_id.strip()
        if not normalized_check_id:
            raise ValueError("check_id 不能为空。")
        normalized_run_id = validate_run_id(run_id)
        status = await self.status()
        if status.code is HarnessStatusCode.MISSING:
            return _unavailable_check_result(
                check_id=normalized_check_id,
                run_id=normalized_run_id,
                message="当前工作区尚未配置 Harness Profile，检查未执行。",
            )
        if status.code is HarnessStatusCode.INVALID:
            return _unavailable_check_result(
                check_id=normalized_check_id,
                run_id=normalized_run_id,
                message="Harness Profile 无效，检查未执行；请先运行 /harness doctor。",
            )
        if not status.trusted or not status.profile_digest:
            return _unavailable_check_result(
                check_id=normalized_check_id,
                run_id=normalized_run_id,
                profile_digest=status.profile_digest or "-",
                message=(
                    "Harness Profile 未受信任，检查未执行。"
                    "下一步：运行 /harness trust 查看并确认命令。"
                ),
            )
        profile = status.snapshot.profile
        assert profile is not None
        check = next(
            (item for item in profile.checks if item.id == normalized_check_id),
            None,
        )
        if check is None:
            return _unavailable_check_result(
                check_id=normalized_check_id,
                run_id=normalized_run_id,
                profile_digest=status.profile_digest,
                message=f"Harness Profile 未声明检查 {normalized_check_id}。",
            )
        trusted_digest = status.profile_digest

        async def profile_is_current() -> bool:
            current = await self.status()
            return current.trusted and current.profile_digest == trusted_digest

        result = await self._check_runner.run(
            run_id=normalized_run_id,
            check=check,
            profile_digest=trusted_digest,
            profile_is_current=profile_is_current,
        )
        await self._record_check_result(result)
        await self._persist_check_result(result, argv=check.argv)
        return result

    async def list_check_results(self, run_id: str) -> tuple[HarnessCheckResult, ...]:
        """Return the latest result per check for one bounded run history."""
        normalized_run_id = validate_run_id(run_id)
        async with self._check_results_lock:
            results = self._check_results.get(normalized_run_id)
            if results is None:
                return ()
            self._check_results.move_to_end(normalized_run_id)
            return tuple(results.values())

    async def observe_tool_event(
        self,
        *,
        run_id: str,
        event: str,
        data: Mapping[str, Any],
    ) -> HarnessEvidenceRef | None:
        """Collect one tool event for a live, durably started Harness run."""
        collector = self._evidence_collector
        if collector is None or not await self._run_is_persisted(run_id):
            return None
        try:
            return await collector.observe(run_id=run_id, event=event, data=data)
        except HarnessStoreError:
            await self._record_persistence_warning(run_id)
            return None

    async def list_evidence_refs(self, run_id: str) -> tuple[HarnessEvidenceRef, ...]:
        collector = self._evidence_collector
        if collector is None:
            return ()
        return await collector.list_refs(run_id)

    async def explain_run(self, run_id: str | None = None) -> HarnessExplainLookup:
        """Explain one durable run without exposing records from other workspaces."""
        store = self._store
        if store is None:
            return HarnessExplainLookup(
                status="unavailable",
                message=(
                    "Harness 状态库尚未初始化。请重启 NaumiAgent；若问题持续，"
                    "运行 `/harness doctor` 检查用户状态目录。"
                ),
            )
        normalized_run_id = None
        if run_id is not None and run_id.strip().lower() != "latest":
            normalized_run_id = validate_run_id(run_id)
        try:
            if normalized_run_id is None:
                runs = await store.list_runs(self.workspace_root, limit=1)
                stored_run = runs[0] if runs else None
            else:
                stored_run = await store.get_run(normalized_run_id)
                if stored_run is not None and (
                    Path(stored_run.workspace_root).resolve() != self.workspace_root
                ):
                    stored_run = None
        except HarnessStoreError:
            return HarnessExplainLookup(
                status="unavailable",
                message=(
                    "Harness 运行记录损坏或暂时无法读取。请检查用户状态目录权限，"
                    "然后运行 `/harness doctor`。"
                ),
            )
        if stored_run is None:
            return HarnessExplainLookup(
                status="not_found",
                message=(
                    "当前工作区没有匹配的 Harness 运行记录。"
                    "请先执行一次任务，或检查 run id 是否属于当前工作区。"
                ),
            )
        return HarnessExplainLookup(
            status="ok",
            explanation=self._explainer.explain(stored_run),
        )

    async def replay_run(self, run_id: str | None = None) -> HarnessReplayLookup:
        """Safely replay durable facts without tools, models, checks, or sessions."""
        store = self._store
        if store is None:
            return HarnessReplayLookup(
                status="unavailable",
                message=(
                    "Harness 状态库尚未初始化。请重启 NaumiAgent；若问题持续，"
                    "运行 `/harness doctor` 检查用户状态目录。"
                ),
            )
        normalized_run_id = None
        if run_id is not None and run_id.strip().lower() != "latest":
            normalized_run_id = validate_run_id(run_id)
        try:
            if normalized_run_id is None:
                runs = await store.list_runs(self.workspace_root, limit=1)
                stored_run = runs[0] if runs else None
            else:
                stored_run = await store.get_run(normalized_run_id)
                if stored_run is not None and (
                    Path(stored_run.workspace_root).resolve() != self.workspace_root
                ):
                    stored_run = None
            if stored_run is None:
                return HarnessReplayLookup(
                    status="not_found",
                    message=(
                        "当前工作区没有匹配的 Harness 运行记录。"
                        "请先执行一次任务，或检查 run id 是否属于当前工作区。"
                    ),
                )
            baseline = await store.get_replay_baseline(stored_run.id)
            legacy_created = baseline is None
            if baseline is None:
                payload = await asyncio.to_thread(
                    capture_replay_baseline,
                    stored_run,
                    workspace_root=self.workspace_root,
                )
                baseline = await store.record_replay_baseline(
                    payload,
                    created_at=stored_run.completed_at or stored_run.started_at,
                )
            result = await asyncio.to_thread(
                replay_stored_run,
                stored_run,
                baseline=baseline,
                workspace_root=self.workspace_root,
                legacy_baseline_created=legacy_created,
            )
        except HarnessStoreError:
            return HarnessReplayLookup(
                status="unavailable",
                message=(
                    "Harness Replay 数据损坏或暂时无法读取。"
                    "请检查用户状态目录权限，然后运行 `/harness doctor`。"
                ),
            )
        except (OSError, RuntimeError, ValueError):
            return HarnessReplayLookup(
                status="unavailable",
                message=(
                    "Harness Replay 无法安全重建。记录不会被执行或自动修复；"
                    "请运行 `/harness doctor` 检查状态库。"
                ),
            )
        return HarnessReplayLookup(status="ok", result=result)

    async def begin_completion_run(
        self,
        *,
        task: str,
        run_id: str,
        session_id: str,
    ) -> HarnessRunState | None:
        """Create an ephemeral completion contract for one trusted run."""
        status = await self.status()
        profile = status.snapshot.profile
        if not status.trusted or profile is None or status.profile_digest is None:
            return None
        try:
            initial_tree = await asyncio.to_thread(
                compute_tree_fingerprint,
                self.workspace_root,
            )
        except TreeFingerprintError:
            initial_tree = TreeFingerprint(
                digest="unavailable",
                head="",
                dirty_paths=(),
                path_digests=(),
            )
        contract = build_completion_contract(
            run_id=validate_run_id(run_id),
            session_id=session_id,
            profile_digest=status.profile_digest,
            task_kind=HarnessTaskKind.ANALYSIS,
            objective=task,
            correction_attempts=profile.completion.correction_attempts,
            unverified_status=profile.completion.unverified_status,
            source_refs=("user:current",),
        )
        available_check_ids = tuple(check.id for check in profile.checks)
        state = HarnessRunState(
            contract=contract,
            initial_tree=initial_tree,
            available_check_ids=available_check_ids,
            context=render_completion_contract_context(
                contract,
                available_check_ids=available_check_ids,
            ),
        )
        await self._persist_run_start(state, status=status)
        return state

    async def evaluate_completion_run(
        self,
        state: HarnessRunState,
        *,
        pending_todo_ids: tuple[str, ...] = (),
        known_failure_ids: tuple[str, ...] = (),
        disclosed_failure_ids: tuple[str, ...] = (),
        evidence: tuple[HarnessEvidenceRef, ...] = (),
    ) -> CompletionGateResult:
        """Evaluate current mechanical evidence and update the run state."""
        if state.finalized and state.receipt is not None:
            return CompletionGateResult(
                status=state.receipt.status,
                receipt=state.receipt,
            )
        infrastructure_errors: tuple[str, ...] = ()
        try:
            current_tree = await asyncio.to_thread(
                compute_tree_fingerprint,
                self.workspace_root,
            )
            changed_paths = await asyncio.to_thread(
                changed_paths_between,
                self.workspace_root,
                state.initial_tree,
                current_tree,
            )
        except TreeFingerprintError as exc:
            current_tree = state.initial_tree
            changed_paths = ()
            infrastructure_errors = (str(exc),)

        status = await self.status()
        current_profile_digest = status.profile_digest
        if (
            status.trusted
            and status.snapshot.profile is not None
            and current_profile_digest == state.contract.profile_digest
        ):
            effective_kind = (
                HarnessTaskKind.CHANGE
                if state.mutating_tool_used or changed_paths
                else state.contract.task_kind
            )
            if state.mutating_tool_used and not changed_paths:
                required_checks = tuple(
                    check.id
                    for check in status.snapshot.profile.checks
                    if effective_kind.value in check.required_for
                )
            else:
                required_checks = select_required_check_ids(
                    status.snapshot.profile.checks,
                    task_kind=effective_kind.value,
                    changed_paths=changed_paths,
                )
            state.contract = state.contract.model_copy(
                update={
                    "task_kind": effective_kind,
                    "required_checks": required_checks,
                }
            )
            state.context = render_completion_contract_context(
                state.contract,
                available_check_ids=state.available_check_ids,
            )

        collected_evidence = await self.list_evidence_refs(state.contract.run_id)
        merged_evidence = _merge_evidence_refs(evidence, collected_evidence)
        result = self._completion_gate.evaluate(
            state.contract,
            CompletionGateInput(
                current_tree_fingerprint=current_tree.digest,
                current_profile_digest=current_profile_digest,
                changed_paths=changed_paths,
                checks=await self.list_check_results(state.contract.run_id),
                evidence=merged_evidence,
                pending_todo_ids=pending_todo_ids,
                known_failure_ids=known_failure_ids,
                disclosed_failure_ids=disclosed_failure_ids,
                infrastructure_errors=infrastructure_errors,
                informational_warnings=await self._persistence_warnings_for(
                    state.contract.run_id
                ),
                mutating_tool_used=state.mutating_tool_used,
            ),
            correction_attempt=state.correction_attempt,
        )
        if result.status == "needs_correction":
            state.correction_attempt += 1
        else:
            if result.receipt is not None:
                receipt = await self._persist_completion_receipt(
                    state,
                    result.receipt,
                )
                result = result.model_copy(update={"receipt": receipt})
            state.finalized = True
            state.receipt = result.receipt
            await self._forget_persistence_state(state.contract.run_id)
        return result

    async def _record_check_result(self, result: HarnessCheckResult) -> None:
        async with self._check_results_lock:
            run_results = self._check_results.setdefault(
                result.run_id,
                OrderedDict(),
            )
            run_results[result.check_id] = result
            run_results.move_to_end(result.check_id)
            self._check_results.move_to_end(result.run_id)
            while len(self._check_results) > self._max_check_runs:
                self._check_results.popitem(last=False)

    async def _persist_run_start(
        self,
        state: HarnessRunState,
        *,
        status: HarnessStatus,
    ) -> None:
        if self._store is None or status.profile_digest is None:
            return
        now = datetime.now(UTC).isoformat()
        trust = status.stored_trust
        try:
            await self._store.record_profile(
                workspace_root=self.workspace_root,
                profile_digest=status.profile_digest,
                schema_version=1,
                loaded_at=now,
                trusted_at=trust.trusted_at if trust is not None else now,
                trust_source=trust.source if trust is not None else "runtime",
                status="trusted",
            )
            await self._store.start_run(
                workspace_root=self.workspace_root,
                contract=state.contract,
                tree_fingerprint_before=state.initial_tree.digest,
                started_at=now,
            )
        except HarnessStoreError:
            await self._record_persistence_warning(state.contract.run_id)
            return
        async with self._store_state_lock:
            self._persisted_run_ids[state.contract.run_id] = None
            self._persisted_run_ids.move_to_end(state.contract.run_id)
            while len(self._persisted_run_ids) > self._max_persisted_runs:
                self._persisted_run_ids.popitem(last=False)

    async def _persist_check_result(
        self,
        result: HarnessCheckResult,
        *,
        argv: tuple[str, ...],
    ) -> None:
        if self._store is None or not await self._run_is_persisted(result.run_id):
            return
        completed = datetime.now(UTC)
        started = completed - timedelta(milliseconds=max(result.duration_ms, 0))
        try:
            await self._store.record_check(
                result=result,
                argv=argv,
                cwd=self.workspace_root,
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
            )
        except HarnessStoreError:
            await self._record_persistence_warning(result.run_id)

    async def _persist_completion_receipt(
        self,
        state: HarnessRunState,
        receipt: HarnessCompletionReceipt,
    ) -> HarnessCompletionReceipt:
        run_id = state.contract.run_id
        if self._store is None or not await self._run_is_persisted(run_id):
            return receipt
        try:
            await self._store.finish_run(
                run_id=run_id,
                receipt=receipt,
                completed_at=datetime.now(UTC).isoformat(),
                contract=state.contract,
            )
            return receipt
        except HarnessStoreError:
            await self._record_persistence_warning(run_id)
            return receipt.model_copy(
                update={
                    "warnings": tuple(
                        dict.fromkeys((*receipt.warnings, _STORE_WARNING))
                    )
                }
            )

    async def _record_persistence_warning(self, run_id: str) -> None:
        async with self._store_state_lock:
            warnings = self._store_warnings.setdefault(run_id, [])
            if _STORE_WARNING not in warnings:
                warnings.append(_STORE_WARNING)
            self._store_warnings.move_to_end(run_id)
            while len(self._store_warnings) > self._max_persisted_runs:
                self._store_warnings.popitem(last=False)

    async def _persistence_warnings_for(self, run_id: str) -> tuple[str, ...]:
        async with self._store_state_lock:
            warnings = self._store_warnings.get(run_id, ())
            return tuple(warnings)

    async def _run_is_persisted(self, run_id: str) -> bool:
        async with self._store_state_lock:
            return run_id in self._persisted_run_ids

    async def _forget_persistence_state(self, run_id: str) -> None:
        async with self._store_state_lock:
            self._persisted_run_ids.pop(run_id, None)
            self._store_warnings.pop(run_id, None)
        if self._evidence_collector is not None:
            await self._evidence_collector.forget_run(run_id)

    async def required_check_ids(
        self,
        *,
        task_kind: str,
        changed_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Select required checks from the current trusted Profile."""
        if task_kind not in {"answer", "analysis", "change", "monitor"}:
            raise ValueError("task_kind 必须是 answer、analysis、change 或 monitor。")
        status = await self.status()
        if not status.trusted or status.snapshot.profile is None:
            raise ValueError("Harness Profile 未受信任，不能选择必需检查。")
        return select_required_check_ids(
            status.snapshot.profile.checks,
            task_kind=task_kind,
            changed_paths=changed_paths,
        )

    async def status(self) -> HarnessStatus:
        snapshot = self._load()
        if snapshot.status is HarnessProfileStatus.MISSING:
            return HarnessStatus(
                code=HarnessStatusCode.MISSING,
                snapshot=snapshot,
                trusted=False,
            )
        if snapshot.status is HarnessProfileStatus.INVALID:
            stored, available = await self._read_trust()
            return HarnessStatus(
                code=HarnessStatusCode.INVALID,
                snapshot=snapshot,
                trusted=False,
                stored_trust=stored,
                trust_store_available=available,
            )

        stored, available = await self._read_trust()
        trusted = stored is not None and stored.profile_digest == snapshot.digest
        return HarnessStatus(
            code=(HarnessStatusCode.TRUSTED if trusted else HarnessStatusCode.UNTRUSTED),
            snapshot=snapshot,
            trusted=trusted,
            stored_trust=stored,
            trust_store_available=available,
        )

    async def doctor(self) -> HarnessDoctorReport:
        status = await self.status()
        findings: list[HarnessDoctorFinding] = []
        commands: list[str] = []

        if status.code is HarnessStatusCode.MISSING:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_missing",
                    level="info",
                    message="当前工作区尚未配置 .naumi/harness.yaml。",
                    hint="创建 schema_version: 1 的配置后重新运行 /harness doctor。",
                )
            )
            return HarnessDoctorReport(status, tuple(findings), ())

        if status.code is HarnessStatusCode.INVALID:
            findings.extend(
                HarnessDoctorFinding(
                    code=error.code,
                    level="error",
                    message=error.message,
                    hint=error.hint,
                )
                for error in status.snapshot.errors
            )
            return HarnessDoctorReport(status, tuple(findings), ())

        findings.append(
            HarnessDoctorFinding(
                code="profile_valid",
                level="ok",
                message="Harness Profile schema version 1 解析通过。",
            )
        )
        if not status.trust_store_available:
            findings.append(
                HarnessDoctorFinding(
                    code="trust_store_unavailable",
                    level="warning",
                    message="用户级 Harness 信任状态暂时不可用。",
                    hint="检查用户状态目录权限；Profile 命令仍不会执行。",
                )
            )
        elif status.trusted:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_trusted",
                    level="ok",
                    message="当前 Profile digest 已由用户信任。",
                )
            )
        else:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_untrusted",
                    level="warning",
                    message="当前 Profile digest 尚未受信任。",
                    hint="运行 /harness trust 预览，再运行 /harness trust --confirm。",
                )
            )

        profile = status.snapshot.profile
        assert profile is not None
        for path_text in profile.knowledge.entrypoints:
            path = (self.workspace_root / path_text).resolve(strict=False)
            exists = path.is_file()
            findings.append(
                HarnessDoctorFinding(
                    code="entrypoint_ok" if exists else "entrypoint_missing",
                    level="ok" if exists else "warning",
                    message=(
                        f"知识入口可读取：{path_text}"
                        if exists
                        else f"知识入口不存在：{path_text}"
                    ),
                    hint="" if exists else "创建文件或从 knowledge.entrypoints 中移除。",
                )
            )
        for path_text in profile.evals.suites:
            path = (self.workspace_root / path_text).resolve(strict=False)
            exists = path.is_file()
            findings.append(
                HarnessDoctorFinding(
                    code="eval_suite_ok" if exists else "eval_suite_missing",
                    level="ok" if exists else "warning",
                    message=(
                        f"Eval Suite 可读取：{path_text}"
                        if exists
                        else f"Eval Suite 尚不存在：{path_text}"
                    ),
                    hint="" if exists else "H5 前可以保留为待建设入口。",
                )
            )
        for check in profile.checks:
            command = shlex.join(check.argv)
            commands.append(f"{check.id}: {command}")
            findings.append(
                HarnessDoctorFinding(
                    code="check_declared",
                    level="info",
                    message=f"已声明检查 {check.id}：{command}",
                )
            )

        findings.append(
            HarnessDoctorFinding(
                code=("execution_enabled" if status.trusted else "execution_disabled"),
                level="info",
                message=(
                    "受信任的 Profile 检查可通过 /harness check <id> 按需执行；"
                    "系统不会自动运行全部检查。"
                    if status.trusted
                    else "Profile 未受信任，不会执行其中的任何命令。"
                ),
            )
        )
        return HarnessDoctorReport(status, tuple(findings), tuple(commands))

    async def trust(self, *, source: str) -> HarnessTrustRecord:
        snapshot = self._load()
        if snapshot.status is HarnessProfileStatus.MISSING:
            raise ValueError("Harness 配置不存在，无法建立信任。")
        if snapshot.status is HarnessProfileStatus.INVALID or snapshot.digest is None:
            raise ValueError("Harness 配置无效，修复后才能建立信任。")
        return await self._trust_store.trust(
            self.workspace_root,
            snapshot.digest,
            source=source,
        )

    async def untrust(self) -> bool:
        return await self._trust_store.untrust(self.workspace_root)

    async def knowledge_context(
        self,
        task: str,
        *,
        model_window: int | None,
    ) -> HarnessKnowledgeContextResult:
        """Compose trusted repository knowledge without persisting its body."""
        started = time.perf_counter()
        status = await self.status()
        unavailable = _knowledge_unavailable(status)
        if unavailable is not None:
            return _with_elapsed(unavailable, started)

        profile = status.snapshot.profile
        digest = status.snapshot.digest
        assert profile is not None and digest is not None
        try:
            snapshot, cache_hit = await self._get_knowledge_snapshot(
                profile,
                digest,
            )
            if not await self._profile_trust_is_current(digest):
                return _with_elapsed(
                    HarnessKnowledgeContextResult(
                        code=HarnessKnowledgeStatusCode.UNTRUSTED,
                        bundle=None,
                        message=(
                            "Harness Profile 在知识组装期间发生变化；"
                            "请重新运行 /harness trust 预览并确认。"
                        ),
                    ),
                    started,
                )
            selection_key = (snapshot.fingerprint, task, model_window)
            bundle = self._selection_cache.get(selection_key)
            selection_cache_hit = bundle is not None
            if (
                bundle is not None
                and not await asyncio.to_thread(
                    self._knowledge_index.sources_are_current,
                    snapshot,
                    bundle.source_paths,
                )
            ):
                await self.invalidate_knowledge_cache()
                snapshot, cache_hit = await self._get_knowledge_snapshot(
                    profile,
                    digest,
                )
                if not await self._profile_trust_is_current(digest):
                    return _with_elapsed(
                        HarnessKnowledgeContextResult(
                            code=HarnessKnowledgeStatusCode.UNTRUSTED,
                            bundle=None,
                            message=(
                                "Harness Profile 在知识重建期间发生变化；"
                                "请重新运行 /harness trust 预览并确认。"
                            ),
                        ),
                        started,
                    )
                selection_key = (snapshot.fingerprint, task, model_window)
                bundle = None
                selection_cache_hit = False
            if bundle is None:
                bundle = await asyncio.to_thread(
                    self._knowledge_composer.compose,
                    task,
                    snapshot,
                    profile,
                    model_window=model_window,
                )
                self._selection_cache[selection_key] = bundle
                self._selection_cache.move_to_end(selection_key)
                while len(self._selection_cache) > 16:
                    self._selection_cache.popitem(last=False)
            else:
                self._selection_cache.move_to_end(selection_key)
        except Exception:
            return _with_elapsed(
                HarnessKnowledgeContextResult(
                    code=HarnessKnowledgeStatusCode.ERROR,
                    bundle=None,
                    message=(
                        "仓库知识索引暂时不可用；主任务可以继续。"
                        "下一步：运行 /harness doctor 检查路径与权限。"
                    ),
                ),
                started,
            )
        return _with_elapsed(
            HarnessKnowledgeContextResult(
                code=HarnessKnowledgeStatusCode.READY,
                bundle=bundle,
                message="受信任仓库知识已按当前任务和模型窗口组装。",
                cache_hit=cache_hit,
                selection_cache_hit=selection_cache_hit,
                index_fingerprint=snapshot.fingerprint,
            ),
            started,
        )

    async def read_knowledge(
        self,
        *,
        query: str | None = None,
        path: str | None = None,
        max_tokens: int = 4_000,
    ) -> KnowledgeReadResult:
        """Read one trusted L2 knowledge item through the current cache."""
        status = await self.status()
        if status.code is HarnessStatusCode.MISSING:
            return _unavailable_read(
                "missing",
                max_tokens,
                "Harness Profile 不存在；先创建 .naumi/harness.yaml。",
            )
        if status.code is HarnessStatusCode.INVALID:
            return _unavailable_read(
                "invalid",
                max_tokens,
                "Harness Profile 无效；先运行 /harness doctor 修复。",
            )
        if status.code is not HarnessStatusCode.TRUSTED:
            return _unavailable_read(
                "untrusted",
                max_tokens,
                "仓库知识尚未受信任；先运行 /harness trust 预览并确认。",
            )
        profile = status.snapshot.profile
        digest = status.snapshot.digest
        assert profile is not None and digest is not None
        try:
            snapshot, _ = await self._get_knowledge_snapshot(profile, digest)
            current_status = await self.status()
            if (
                current_status.code is not HarnessStatusCode.TRUSTED
                or current_status.snapshot.digest != digest
            ):
                return _unavailable_read(
                    "untrusted",
                    max_tokens,
                    "Profile 已变化；重新运行 /harness trust 后再读取知识。",
                )
            return await asyncio.to_thread(
                self._knowledge_index.read,
                snapshot,
                query=query,
                path=path,
                max_tokens=max_tokens,
            )
        except ValueError:
            raise
        except Exception:
            return _unavailable_read(
                "invalid",
                max_tokens,
                "知识索引读取失败；运行 /harness doctor 检查路径与权限。",
            )

    async def _get_knowledge_snapshot(
        self,
        profile: HarnessProfile,
        digest: str,
    ) -> tuple[KnowledgeIndexSnapshot, bool]:
        cached = self._knowledge_cache
        if (
            cached is not None
            and cached.profile_digest == digest
            and await self._cached_snapshot_is_current(cached)
        ):
            return cached, True

        async with self._knowledge_lock:
            current_cache = self._knowledge_cache
            if (
                current_cache is not None
                and current_cache is not cached
                and current_cache.profile_digest == digest
            ):
                return current_cache, True
            if self._knowledge_build is not None and self._knowledge_build[0] == digest:
                build_task = self._knowledge_build[1]
            else:
                build_task = asyncio.create_task(asyncio.to_thread(
                    self._knowledge_index.build,
                    profile,
                    profile_digest=digest,
                ))
                self._knowledge_build = (digest, build_task)

        try:
            built = await build_task
        except BaseException:
            async with self._knowledge_lock:
                if (
                    self._knowledge_build is not None
                    and self._knowledge_build[1] is build_task
                ):
                    self._knowledge_build = None
            raise
        async with self._knowledge_lock:
            existing = self._knowledge_cache
            if (
                existing is not None
                and existing.profile_digest == digest
                and existing is not cached
            ):
                return existing, True
            if (
                self._knowledge_build is not None
                and self._knowledge_build[1] is not build_task
            ):
                return built, False
            self._knowledge_cache = built
            self._selection_cache.clear()
            self._last_git_audit_at = time.monotonic()
            if (
                self._knowledge_build is not None
                and self._knowledge_build[1] is build_task
            ):
                self._knowledge_build = None
        return built, False

    async def _cached_snapshot_is_current(
        self,
        snapshot: KnowledgeIndexSnapshot,
    ) -> bool:
        metadata_current = await asyncio.to_thread(
            self._knowledge_index.metadata_is_current,
            snapshot,
        )
        if not metadata_current:
            return False
        now = time.monotonic()
        if now - self._last_git_audit_at < self._git_audit_interval_seconds:
            return True
        current = await asyncio.to_thread(self._knowledge_index.is_current, snapshot)
        self._last_git_audit_at = now
        return current

    async def invalidate_knowledge_cache(self) -> None:
        """Invalidate repository knowledge after an in-process write operation."""
        async with self._knowledge_lock:
            self._knowledge_cache = None
            self._selection_cache.clear()
            self._last_git_audit_at = 0.0

    async def _profile_trust_is_current(self, digest: str) -> bool:
        status = await self.status()
        return (
            status.code is HarnessStatusCode.TRUSTED
            and status.snapshot.digest == digest
        )

    def _load(self) -> HarnessProfileSnapshot:
        return load_harness_profile(self.workspace_root, self._profile_path)

    async def _read_trust(self) -> tuple[HarnessTrustRecord | None, bool]:
        try:
            return await self._trust_store.get(self.workspace_root), True
        except HarnessTrustStoreError:
            return None, False


def render_harness_status(status: HarnessStatus) -> str:
    snapshot = status.snapshot
    if status.code is HarnessStatusCode.MISSING:
        return (
            "## Harness 尚未配置\n\n"
            f"配置路径：`{snapshot.profile_path}`\n\n"
            "下一步：创建 `.naumi/harness.yaml`，然后运行 `/harness doctor`。"
        )
    if status.code is HarnessStatusCode.INVALID:
        errors = "\n".join(
            f"- {error.message} {error.hint}" for error in snapshot.errors
        )
        return (
            "## Harness 配置无效\n\n"
            f"配置路径：`{snapshot.profile_path}`\n\n{errors}"
        )

    assert snapshot.profile is not None
    digest = snapshot.digest or "-"
    if status.code is HarnessStatusCode.UNTRUSTED:
        title = "## Harness 配置未受信任"
        if status.trust_store_available:
            next_step = (
                "下一步：运行 `/harness trust` 查看 digest 与命令摘要，"
                "再运行 `/harness trust --confirm`。"
            )
        else:
            next_step = (
                "用户级信任状态暂时不可用。下一步：检查 NaumiAgent 状态目录权限；"
                "在修复前不会执行 Profile 命令。"
            )
    else:
        title = "## Harness 已就绪"
        next_step = (
            "已启用受信任的仓库知识；可按需运行 Profile 中精确声明的检查。"
        )
    return (
        f"{title}\n\n"
        f"配置路径：`{snapshot.profile_path}`\n"
        f"Profile digest：`{digest}`\n"
        f"检查定义：{len(snapshot.profile.checks)} 条\n\n"
        f"{next_step}"
    )


def render_harness_doctor(report: HarnessDoctorReport) -> str:
    lines = ["## Harness 诊断", "", render_harness_status(report.status), "", "### 检查结果"]
    icons = {"ok": "✅", "info": "ℹ️", "warning": "⚠️", "error": "❌"}
    for finding in report.findings:
        suffix = f"；{finding.hint}" if finding.hint else ""
        lines.append(f"- {icons[finding.level]} {finding.message}{suffix}")
    if report.command_summaries:
        lines.extend(("", "### 配置中的命令"))
        lines.extend(f"- `{command}`" for command in report.command_summaries)
    return "\n".join(lines)


def render_harness_check(result: HarnessCheckResult) -> str:
    """Render a check result without conflating policy and test failures."""
    titles = {
        HarnessCheckStatus.PASSED: "Harness 检查通过",
        HarnessCheckStatus.FAILED: "Harness 检查未通过",
        HarnessCheckStatus.TIMED_OUT: "Harness 检查超时",
        HarnessCheckStatus.CANCELLED: "Harness 检查已取消",
        HarnessCheckStatus.BLOCKED_BY_POLICY: "Harness 检查被安全策略阻止",
        HarnessCheckStatus.INFRASTRUCTURE_ERROR: "Harness 检查基础设施异常",
        HarnessCheckStatus.STALE: "Harness 检查结果已失效",
    }
    lines = [
        f"## {titles[result.status]}",
        "",
        f"- 检查：`{result.check_id}`",
        f"- 状态：`{result.status.value}`",
        f"- Tree fingerprint：`{result.tree_fingerprint}`",
        f"- 耗时：{result.duration_ms}ms",
        f"- 缓存复用：{'是' if result.cached else '否'}",
        "",
        result.message,
    ]
    if result.output:
        safe_output = _safe_check_output(result.output)
        fence = safe_markdown_fence(safe_output)
        lines.extend(("", "### 有界输出尾部", "", fence, safe_output, fence))
    return "\n".join(lines)


def render_harness_knowledge(result: KnowledgeReadResult) -> str:
    """Render one L2 result consistently for slash commands and Agent tools."""
    if result.status == "ok":
        assert result.source is not None
        fence = safe_markdown_fence(result.content)
        truncated = "是" if result.truncated else "否"
        return (
            "## Harness 仓库知识\n\n"
            f"- 来源：`{result.source.path}`\n"
            f"- Knowledge ID：`{result.source.id}`\n"
            f"- Digest：`{result.source.digest}`\n"
            f"- 估算：{result.estimated_tokens}/{result.budget_tokens} tokens\n"
            f"- 已裁剪：{truncated}\n\n"
            f"{fence}\n{result.content}\n{fence}"
        )
    if result.status == "ambiguous":
        candidates = "\n".join(f"- `{path}`" for path in result.candidates)
        return (
            "## Harness 知识查询不唯一\n\n"
            f"{result.message}\n\n候选：\n{candidates}"
        )
    return (
        "## Harness 知识暂不可用\n\n"
        f"状态：`{result.status}`\n\n"
        f"{result.message or '请提供更精确的知识路径或查询。'}"
    )


def render_harness_replay(lookup: HarnessReplayLookup) -> str:
    """Render one bounded Chinese Replay receipt without artifact contents."""
    if lookup.status != "ok" or lookup.result is None:
        title = (
            "没有找到 Harness Replay"
            if lookup.status == "not_found"
            else "Harness Replay 暂不可用"
        )
        return f"## {title}\n\n{lookup.message}"
    result = lookup.result
    labels = {
        "reproduced": "已复现",
        "changed": "结果已变化",
        "partial": "部分可回放",
        "corrupt": "检测到损坏",
    }
    next_steps = {
        "reproduced": "无需操作；该运行的持久化事实与分类结果保持一致。",
        "changed": "审查规则版本和解释 digest 差异，再决定是否接受新分类。",
        "partial": "补齐缺失 artifact 或审查不完整事件后再次回放。",
        "corrupt": "停止信任受影响证据；从可信来源恢复状态库或 artifact。",
    }
    lines = [
        "## Harness 安全回放",
        "",
        f"- 状态：**{labels[result.status]}** (`{result.status}`)",
        f"- Run：`{result.run_id}`",
        f"- Manifest：`{result.current_manifest_sha256}`",
        (
            "- 规则版本："
            f"`{result.baseline_rule_version}` → `{result.current_rule_version}`"
        ),
        f"- 时间线事件：{len(result.timeline)}",
        f"- Artifact/证据校验：{len(result.artifacts)}",
    ]
    if result.legacy_baseline_created:
        lines.append("- 注意：本次为旧记录建立了首个基线，不能证明建立前的规则一致性。")
    if result.anomalies:
        lines.extend(("", "### 不完整项"))
        lines.extend(f"- `{item}`" for item in result.anomalies)
    if result.differences:
        lines.extend(("", "### 差异"))
        lines.extend(
            f"- `{item.field}`：`{item.baseline}` → `{item.current}`"
            for item in result.differences
        )
    failed_artifacts = tuple(
        item for item in result.artifacts if item.status != "verified"
    )
    if failed_artifacts:
        lines.extend(("", "### 证据校验"))
        lines.extend(
            f"- `{item.id}`：`{item.status}`（`{item.reference}`）"
            for item in failed_artifacts
        )
    lines.extend(("", f"下一步：{next_steps[result.status]}"))
    return "\n".join(lines)


def _knowledge_unavailable(
    status: HarnessStatus,
) -> HarnessKnowledgeContextResult | None:
    if status.code is HarnessStatusCode.MISSING:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.MISSING,
            bundle=None,
            message="当前工作区没有 Harness Profile；不会注入仓库知识。",
        )
    if status.code is HarnessStatusCode.INVALID:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.INVALID,
            bundle=None,
            message="Harness Profile 无效；不会注入仓库知识。",
        )
    if status.code is HarnessStatusCode.UNTRUSTED:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.UNTRUSTED,
            bundle=None,
            message="Harness Profile 未受信任；不会注入仓库知识。",
        )
    return None


def _merge_evidence_refs(
    explicit: tuple[HarnessEvidenceRef, ...],
    collected: tuple[HarnessEvidenceRef, ...],
) -> tuple[HarnessEvidenceRef, ...]:
    merged: OrderedDict[str, HarnessEvidenceRef] = OrderedDict()
    for evidence in (*explicit, *collected):
        existing = merged.get(evidence.id)
        if existing is not None and existing != evidence:
            raise ValueError(f"evidence id {evidence.id} 对应多个不同证据。")
        merged[evidence.id] = evidence
    return tuple(merged.values())


def _unavailable_check_result(
    *,
    check_id: str,
    run_id: str,
    message: str,
    profile_digest: str = "-",
) -> HarnessCheckResult:
    return HarnessCheckResult(
        check_id=check_id,
        run_id=run_id,
        status=HarnessCheckStatus.BLOCKED_BY_POLICY,
        tree_fingerprint="-",
        profile_digest=profile_digest,
        message=message,
    )


def _eval_batch_error(
    batch_id: str,
    suite_id: str,
    requested: int,
    code: str,
    message: str,
    *,
    completed: int = 0,
    persisted: int = 0,
    duration_ms: float = 0,
) -> HarnessEvalBatchStatus:
    return HarnessEvalBatchStatus(
        status="error",
        code=code,
        message=message,
        batch_id=batch_id,
        suite_id=suite_id,
        requested=requested,
        completed=completed,
        persisted=persisted,
        duration_ms=duration_ms,
    )


def _eval_promotion_error(
    suite_id: str,
    batch_id: str,
    code: str,
    message: str,
) -> HarnessEvalPromotionStatus:
    return HarnessEvalPromotionStatus(
        status="error",
        code=code,
        message=message,
        suite_id=suite_id,
        batch_id=batch_id,
    )


def _eval_candidate_complete(
    records: tuple[HarnessStoredEvalResult, ...],
) -> bool:
    if [item.sample_index for item in records] != list(range(len(records))):
        return False
    identities = {item.identity_sha256 for item in records}
    if "" in identities or len(identities) != 1:
        return False
    return all(
        item.result.baseline_identity is not None
        and not item.result.baseline_identity_code
        and item.result.baseline_identity.configuration.repetitions == len(records)
        for item in records
    )


def _eval_comparison_error(
    suite_id: str,
    candidate_batch_id: str,
    code: str,
    message: str,
) -> HarnessEvalComparisonRunStatus:
    return HarnessEvalComparisonRunStatus(
        status="error",
        code=code,
        message=message,
        suite_id=suite_id,
        candidate_batch_id=candidate_batch_id,
    )


def _eval_comparison_status(
    stored: HarnessStoredEvalComparisonReceipt,
    baseline_version: int,
    *,
    status: Literal["created", "existing", "stale_baseline"],
) -> HarnessEvalComparisonRunStatus:
    receipt = stored.receipt
    policy_failed = sum(
        item.policy_verdict.value == "failed" for item in receipt.sample_evidence
    )
    policy_inconclusive = sum(
        item.policy_verdict.value in {"inconclusive", "incompatible"}
        for item in receipt.sample_evidence
    )
    return HarnessEvalComparisonRunStatus(
        status=status,
        suite_id=receipt.suite_id,
        baseline_id=receipt.baseline_id,
        baseline_version=baseline_version,
        baseline_batch_id=receipt.baseline_batch_id,
        candidate_batch_id=receipt.current_batch_id,
        baseline_samples=receipt.baseline_samples,
        candidate_samples=receipt.current_samples,
        receipt_id=receipt.id,
        decision=receipt.decision.value,
        statistical_verdict=receipt.statistical_verdict.value,
        policy_failed_samples=policy_failed,
        policy_inconclusive_samples=policy_inconclusive,
        created_at=receipt.created_at,
    )


def _safe_check_output(output: str, *, max_chars: int = 12_000) -> str:
    redacted = OutputGuardrail.redact(output)
    sanitized = "".join(
        character
        if character in {"\n", "\t"}
        or (ord(character) >= 32 and not 127 <= ord(character) <= 159)
        else "�"
        for character in redacted
    )
    if len(sanitized) <= max_chars:
        return sanitized
    return "… 已裁剪，仅显示输出尾部 …\n" + sanitized[-max_chars:]


def _with_elapsed(
    result: HarnessKnowledgeContextResult,
    started: float,
) -> HarnessKnowledgeContextResult:
    return HarnessKnowledgeContextResult(
        code=result.code,
        bundle=result.bundle,
        message=result.message,
        cache_hit=result.cache_hit,
        selection_cache_hit=result.selection_cache_hit,
        index_fingerprint=result.index_fingerprint,
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1_000)),
    )


def _unavailable_read(
    status: Literal["missing", "invalid", "untrusted"],
    max_tokens: int,
    message: str,
) -> KnowledgeReadResult:
    return KnowledgeReadResult(
        status=status,
        content="",
        source=None,
        estimated_tokens=0,
        budget_tokens=max_tokens,
        truncated=False,
        message=message,
    )
