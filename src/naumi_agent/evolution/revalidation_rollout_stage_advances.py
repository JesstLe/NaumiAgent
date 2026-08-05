"""Authorize one exact rollout stage transition without deploying it."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_completions import (
    EvolutionRevalidationRolloutStageCompletion,
    EvolutionRevalidationRolloutStageCompletionError,
    EvolutionRevalidationRolloutStageCompletionService,
    EvolutionRevalidationRolloutStageCompletionStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ADVANCE_POLICY = (
    "evolution-revalidation-rollout-stage-advance-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_INTERACTION_RE = re.compile(r"^ask-evrerolloutadvance-([0-9a-f]{24})-([1-9][0-9]{0,3})$")
_MAX_ARTIFACT_BYTES = 512 * 1024

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRolloutStageAdvanceReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-stage-advance-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ADVANCE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrerolloutadvance_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    completion_id: str = Field(pattern=r"^evrerolloutcomplete_[0-9a-f]{24}$")
    completion_sha256: str = Field(pattern=_SHA256_RE)
    observation_id: str = Field(pattern=r"^evreruntimeobs_[0-9a-f]{24}$")
    observation_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    entry_receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    entry_receipt_sha256: str = Field(pattern=_SHA256_RE)
    completed_stage: Literal["local_canary"] = "local_canary"
    next_stage: Literal["opt_in"] = "opt_in"
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    decision: Literal["advance", "decline"]
    decision_source: Literal["automatic", "manual"]
    interaction: HarnessInteractionRecord | None = None
    interaction_request_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    stage_completion_current: Literal[True] = True
    control_active: Literal[True] = True
    advance_authorized: bool
    next_stage_entry_authority: bool
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stage Advance workspace 必须 canonical。")
        if bool(self.control_sequence) is not bool(self.control_event_sha256):
            raise ValueError("Stage Advance control projection 不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if not issued < expires:
            raise ValueError("Stage Advance 有效期无效。")
        authorized = self.decision == "advance"
        if not (
            self.advance_authorized is authorized
            and self.next_stage_entry_authority is authorized
        ):
            raise ValueError("Stage Advance authority projection 不一致。")
        if self.decision_source == "automatic":
            if self.interaction is not None or self.interaction_request_sha256:
                raise ValueError("自动 Stage Advance 不得伪造用户交互。")
            if self.decision != "advance":
                raise ValueError("自动 Stage Advance 只能投影 advance。")
        else:
            interaction = self.interaction
            if interaction is None or not (
                interaction.state == "answered"
                and interaction.answer_kind == "option"
                and interaction.answer_value == self.decision
                and interaction.answered_by == "user"
                and interaction.subject_kind == "tool"
                and interaction.subject_id == self.completion_id
            ):
                raise ValueError("手动 Stage Advance 未绑定结构化用户答案。")
            match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
            if match is None or match.group(1) != self.completion_id.removeprefix(
                "evrerolloutcomplete_"
            ):
                raise ValueError("Stage Advance interaction identity 无效。")
            request_digest = _digest(_static_request_payload(interaction.request()))
            if not hmac.compare_digest(request_digest, self.interaction_request_sha256):
                raise ValueError("Stage Advance interaction request 摘要不一致。")
            if _aware(interaction.answered_at) != issued:
                raise ValueError("Stage Advance issued_at 未绑定用户答案。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evrerolloutadvance_{digest[:24]}"
        ):
            raise ValueError("Stage Advance identity 不一致。")
        return self


class EvolutionRevalidationRolloutStageAdvanceView(_StrictModel):
    receipt: EvolutionRevalidationRolloutStageAdvanceReceipt
    completion_current: bool
    control_current: bool
    expired: bool
    next_stage_entry_authority: bool

    @model_validator(mode="after")
    def _project(self) -> Self:
        expected = bool(
            self.receipt.next_stage_entry_authority
            and self.completion_current
            and self.control_current
            and not self.expired
        )
        if self.next_stage_entry_authority is not expected:
            raise ValueError("Stage Advance view authority projection 不一致。")
        return self


class EvolutionRevalidationRolloutStageAdvanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRolloutStageAdvanceStore:
    def __init__(self, db_path: str | Path, *, interaction_store: HarnessStore) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Stage Advance Store 需要 Harness Store。")
        self.interaction_store = interaction_store

    async def get_by_completion(self, completion_id: str):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_rollout_stage_advances "
                        "WHERE completion_id = ?",
                        (completion_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(row["receipt_json"])
            await self._require_interaction(item)
            return item
        except EvolutionRevalidationRolloutStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_store_corrupt",
                "Stage Advance Receipt 损坏或无法读取。",
            ) from exc

    async def record(self, receipt):
        try:
            item = EvolutionRevalidationRolloutStageAdvanceReceipt.model_validate_json(
                receipt.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_artifact_invalid", "Stage Advance Receipt 无效。"
            ) from exc
        await self._require_interaction(item)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_oversized", "Stage Advance Receipt 超过 512 KiB。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                completion_row = await (
                    await db.execute(
                        "SELECT completion_sha256, completion_json FROM "
                        "evolution_revalidation_rollout_stage_completions "
                        "WHERE completion_id = ?",
                        (item.completion_id,),
                    )
                ).fetchone()
                control_row = await (
                    await db.execute(
                        "SELECT sequence, event_sha256, state FROM "
                        "evolution_revalidation_rollout_control_events "
                        "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                        (item.workspace_root,),
                    )
                ).fetchone()
                control = (
                    (0, "", "active")
                    if control_row is None
                    else (
                        control_row["sequence"],
                        control_row["event_sha256"],
                        control_row["state"],
                    )
                )
                source_completion = (
                    None
                    if completion_row is None
                    else EvolutionRevalidationRolloutStageCompletion.model_validate_json(
                        completion_row["completion_json"]
                    )
                )
                if not (
                    source_completion is not None
                    and completion_row["completion_sha256"] == item.completion_sha256
                    and _receipt_matches_completion(item, source_completion)
                    and control
                    == (item.control_sequence, item.control_event_sha256, "active")
                ):
                    await db.rollback()
                    raise EvolutionRevalidationRolloutStageAdvanceError(
                        "rollout_stage_advance_dependency_changed",
                        "Stage Completion 或 rollout control 已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_rollout_stage_advances "
                        "WHERE completion_id = ?",
                        (item.completion_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["receipt_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationRolloutStageAdvanceError(
                            "rollout_stage_advance_conflict",
                            "同一 Stage Completion 已绑定不同推进决定。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_rollout_stage_advances "
                    "(receipt_id, receipt_sha256, completion_id, completion_sha256, "
                    "decision, receipt_json, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.completion_id,
                        item.completion_sha256,
                        item.decision,
                        encoded,
                        item.issued_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationRolloutStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_store_error", "Stage Advance Receipt 无法持久化。"
            ) from exc
        return item

    async def _require_interaction(self, item) -> None:
        if item.interaction is None:
            return
        try:
            authoritative = await self.interaction_store.get_interaction(
                workspace_root=item.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_read_failed",
                "无法重读 Stage Advance interaction authority。",
            ) from exc
        if authoritative != item.interaction:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_mismatch",
                "Stage Advance 未绑定 Harness interaction authority。",
            )


class EvolutionRevalidationRolloutStageAdvanceService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_service: EvolutionRevalidationRolloutStageCompletionService,
        completion_store: EvolutionRevalidationRolloutStageCompletionStore,
        control_store: EvolutionRevalidationRolloutControlStore,
        interaction_store: HarnessStore,
        store: EvolutionRevalidationRolloutStageAdvanceStore,
        request_user_input: RequestUserInputCallback,
        validity_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 30 <= validity_seconds <= 86_400:
            raise ValueError("Stage Advance validity 必须在 30..86400 秒。")
        if not callable(request_user_input):
            raise TypeError("Stage Advance service 需要用户交互 callback。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.completion_service = completion_service
        self.completion_store = completion_store
        self.control_store = control_store
        self.interaction_store = interaction_store
        self.store = store
        self.request_user_input = request_user_input
        self.validity_seconds = validity_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def authorize(self, *, completion_id: str):
        lock = self._locks.setdefault(completion_id, asyncio.Lock())
        async with lock:
            completion = await self._current_completion(completion_id)
            existing = await self.store.get_by_completion(completion_id)
            if existing is not None:
                return await self._view(existing, completion)
            if completion.automatic_advance_eligible:
                receipt = await self._build_receipt(
                    completion=completion,
                    decision="advance",
                    decision_source="automatic",
                    interaction=None,
                )
            elif completion.manual_interaction_required:
                interaction = await self._manual_decision(completion)
                completion = await self._current_completion(completion_id)
                receipt = await self._build_receipt(
                    completion=completion,
                    decision=interaction.answer_value,
                    decision_source="manual",
                    interaction=interaction,
                )
            else:
                raise EvolutionRevalidationRolloutStageAdvanceError(
                    "rollout_stage_advance_policy_invalid",
                    "Stage Completion 没有可执行的推进策略。",
                )
            stored = await self.store.record(receipt)
            return await self._view(stored, completion)

    async def inspect(self, *, completion_id: str):
        completion = await self._current_completion(completion_id)
        receipt = await self.store.get_by_completion(completion_id)
        if receipt is None:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_not_found", "尚未形成 Stage Advance Receipt。"
            )
        return await self._view(receipt, completion)

    async def _current_completion(self, completion_id: str):
        completion = await self.completion_store.get(completion_id)
        if completion is None or completion.workspace_root != str(self.workspace_root):
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_completion_not_found",
                "未找到当前工作区的 Stage Completion。",
            )
        try:
            current = await self.completion_service.complete(
                observation_id=completion.observation_id
            )
        except EvolutionRevalidationRolloutStageCompletionError as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_completion_stale",
                "Stage Completion 已失效，不能推进 rollout。",
            ) from exc
        if current != completion:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_completion_stale",
                "Stage Completion 已被不同 evidence 取代。",
            )
        return completion

    async def _manual_decision(self, completion):
        try:
            history = await self.interaction_store.list_interactions(
                workspace_root=self.workspace_root,
                subject_kind="tool",
                subject_ids=(completion.completion_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_history_failed",
                "无法读取 Stage Advance 持久交互历史。",
            ) from exc
        valid = tuple(item for item in history if _interaction_matches(item, completion))
        answered = tuple(item for item in valid if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_answer_ambiguous",
                "同一 Stage Completion 存在多个已回答推进交互。",
            )
        if answered:
            return answered[0]
        pending = next((item for item in valid if item.state == "pending"), None)
        if pending is not None:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_pending",
                f"推进交互 {pending.interaction_id} 仍待回答。",
            )
        request = _interaction_request(completion, timeout_seconds=604_800)
        interaction_id = _next_interaction_id(completion, history)
        try:
            await self.request_user_input(
                {
                    **request.to_public_dict(),
                    "_interaction_id": interaction_id,
                    "_durable_subject_kind": "tool",
                    "_durable_subject_id": completion.completion_id,
                }
            )
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_unavailable",
                "当前界面无法创建持久 Stage Advance 交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_interaction_invalid",
                "Stage Advance 交互未通过运行时协议校验。",
            ) from exc
        interaction = await self.interaction_store.get_interaction(
            workspace_root=self.workspace_root, interaction_id=interaction_id
        )
        if interaction is None or interaction.state != "answered":
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_answer_not_committed",
                "用户答案尚未提交到 Harness authority。",
            )
        if not _interaction_matches(interaction, completion):
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_answer_mismatch",
                "用户答案没有绑定 exact Stage Completion。",
            )
        return interaction

    async def _build_receipt(self, *, completion, decision, decision_source, interaction):
        now = _aware(interaction.answered_at) if interaction is not None else self._now()
        control = await self.control_store.latest(self.workspace_root)
        if (
            control is not None
            and control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
        ):
            raise EvolutionRevalidationRolloutStageAdvanceError(
                "rollout_stage_advance_control_not_active", "Rollout 已暂停，不能推进 stage。"
            )
        control_sequence = 0 if control is None else control.sequence
        control_sha = "" if control is None else control.event_sha256
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ADVANCE_POLICY,
            "workspace_root": str(self.workspace_root),
            "completion_id": completion.completion_id,
            "completion_sha256": completion.completion_sha256,
            "observation_id": completion.observation_id,
            "observation_sha256": completion.observation_sha256,
            "plan_id": completion.plan_id,
            "plan_sha256": completion.plan_sha256,
            "entry_receipt_id": completion.entry_receipt_id,
            "entry_receipt_sha256": completion.entry_receipt_sha256,
            "completed_stage": "local_canary",
            "next_stage": "opt_in",
            "control_sequence": control_sequence,
            "control_event_sha256": control_sha,
            "decision": decision,
            "decision_source": decision_source,
            "interaction": interaction,
            "interaction_request_sha256": (
                ""
                if interaction is None
                else _digest(_static_request_payload(interaction.request()))
            ),
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.validity_seconds)).isoformat(),
            "stage_completion_current": True,
            "control_active": True,
            "advance_authorized": decision == "advance",
            "next_stage_entry_authority": decision == "advance",
            "deployment_authority": False,
            "rollback_authority": False,
            "promotion_authority": False,
            "git_write_executed": False,
            "publish_executed": False,
            "llm_generated": False,
        }
        digest = _digest(core)
        return EvolutionRevalidationRolloutStageAdvanceReceipt.model_validate(
            {**core, "receipt_id": f"evrerolloutadvance_{digest[:24]}", "receipt_sha256": digest}
        )

    async def _view(self, receipt, completion):
        control = await self.control_store.latest(self.workspace_root)
        control_projection = (0, "", "active") if control is None else (
            control.sequence,
            control.event_sha256,
            control.state.value,
        )
        control_current = control_projection == (
            receipt.control_sequence,
            receipt.control_event_sha256,
            "active",
        )
        completion_current = completion.completion_sha256 == receipt.completion_sha256
        expired = self._now() >= _aware(receipt.expires_at)
        return EvolutionRevalidationRolloutStageAdvanceView(
            receipt=receipt,
            completion_current=completion_current,
            control_current=control_current,
            expired=expired,
            next_stage_entry_authority=(
                receipt.next_stage_entry_authority
                and completion_current
                and control_current
                and not expired
            ),
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


def _interaction_request(completion, *, timeout_seconds):
    return normalize_interaction_request(
        {
            "header": "自进化 Rollout 阶段推进",
            "question": (
                f"本地 canary 已通过 {completion.completed_runs} 次运行和 "
                f"{completion.observation_seconds} 秒观察。是否授权候选从 local_canary "
                f"推进到 opt_in？本选择只签发短期 Stage Entry authority，不会部署、发布或推送。"
            ),
            "options": [
                {
                    "value": "advance",
                    "label": "推进到 opt-in",
                    "description": "签发短期且受 rollout control fencing 的下一阶段入口权限。",
                },
                {
                    "value": "decline",
                    "label": "停止推进",
                    "description": "记录拒绝决定；不产生部署或下一阶段入口权限。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义推进决定",
            "timeout_seconds": timeout_seconds,
            "priority": "critical",
        }
    )


def _interaction_matches(interaction, completion) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and match.group(1) == completion.completion_id.removeprefix("evrerolloutcomplete_")
        and interaction.subject_kind == "tool"
        and interaction.subject_id == completion.completion_id
        and _static_request_payload(interaction.request())
        == _static_request_payload(_interaction_request(completion, timeout_seconds=None))
    )


def _receipt_matches_completion(receipt, completion) -> bool:
    expected_source = "manual" if completion.manual_interaction_required else "automatic"
    return bool(
        receipt.workspace_root == completion.workspace_root
        and receipt.completion_id == completion.completion_id
        and receipt.completion_sha256 == completion.completion_sha256
        and receipt.observation_id == completion.observation_id
        and receipt.observation_sha256 == completion.observation_sha256
        and receipt.plan_id == completion.plan_id
        and receipt.plan_sha256 == completion.plan_sha256
        and receipt.entry_receipt_id == completion.entry_receipt_id
        and receipt.entry_receipt_sha256 == completion.entry_receipt_sha256
        and receipt.completed_stage == completion.completed_stage
        and receipt.next_stage == completion.next_stage
        and receipt.decision_source == expected_source
        and (
            completion.manual_interaction_required
            or completion.automatic_advance_eligible
        )
    )


def _next_interaction_id(completion, history) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(2)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionRevalidationRolloutStageAdvanceError(
            "rollout_stage_advance_attempts_exhausted", "Stage Advance 交互次数已达上限。"
        )
    suffix = completion.completion_id.removeprefix("evrerolloutcomplete_")
    return f"ask-evrerolloutadvance-{suffix}-{attempt}"


def _static_request_payload(request: UserInteractionRequest) -> dict[str, object]:
    return {**request.to_public_dict(), "timeout_seconds": None}


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Stage Advance timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def _restore(encoded: str):
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Stage Advance Receipt 超过 512 KiB。")
    return EvolutionRevalidationRolloutStageAdvanceReceipt.model_validate_json(encoded)


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_stage_advances ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "completion_id TEXT NOT NULL UNIQUE, completion_sha256 TEXT NOT NULL, "
        "decision TEXT NOT NULL, receipt_json TEXT NOT NULL, issued_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ADVANCE_POLICY",
    "EvolutionRevalidationRolloutStageAdvanceError",
    "EvolutionRevalidationRolloutStageAdvanceReceipt",
    "EvolutionRevalidationRolloutStageAdvanceService",
    "EvolutionRevalidationRolloutStageAdvanceStore",
    "EvolutionRevalidationRolloutStageAdvanceView",
]
