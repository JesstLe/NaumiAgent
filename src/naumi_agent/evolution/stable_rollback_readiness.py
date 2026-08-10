"""Read-only rollback readiness for one completed stable population."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_deployments import (
    EvolutionRevalidationStableDeploymentView,
)
from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionView,
)
from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore

EVOLUTION_STABLE_ROLLBACK_READINESS_POLICY = (
    "evolution-stable-rollback-readiness-v1"
)
_COMPLETION_RE = re.compile(r"^evstablepopcomplete_[0-9a-f]{24}$")
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")


@runtime_checkable
class EvolutionStablePopulationCompletionInspectionPort(Protocol):
    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStablePopulationCompletionView: ...


@runtime_checkable
class EvolutionStableDeploymentInspectionPort(Protocol):
    async def inspect_stable_deployment(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentView: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRollbackReadiness(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-rollback-readiness-v1"] = (
        EVOLUTION_STABLE_ROLLBACK_READINESS_POLICY
    )
    readiness_id: str = Field(pattern=r"^evstablerollbackready_[0-9a-f]{24}$")
    readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    completion_receipt_id: str = Field(
        pattern=r"^evstablepopcomplete_[0-9a-f]{24}$"
    )
    completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    deployment_receipt_id: str = Field(
        pattern=r"^evrestabledeployment_[0-9a-f]{24}$"
    )
    deployment_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    rollback_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_pointer_generation: int = Field(ge=1, le=999_999_999)
    rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_current: Literal[True] = True
    active_deployment_current: Literal[True] = True
    activation_chain_current: Literal[True] = True
    previous_slot_retained: Literal[True] = True
    previous_slot_bootable: Literal[True] = True
    expected_pointer_cas_ready: Literal[True] = True
    binary_rollback_readiness_authority: Literal[True] = True
    config_data_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    assessed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Rollback Readiness workspace 必须 canonical。")
        if not (
            self.rollback_pointer_generation + 1
            == self.expected_active_pointer_generation
            and self.rollback_slot_id != self.candidate_slot_id
            and _aware(self.assessed_at)
        ):
            raise ValueError("Stable Rollback Readiness pointer projection 不一致。")
        core = self.model_dump(
            mode="json", exclude={"readiness_id", "readiness_sha256"}
        )
        digest = _digest(core)
        if self.readiness_sha256 != digest or self.readiness_id != (
            f"evstablerollbackready_{digest[:24]}"
        ):
            raise ValueError("Stable Rollback Readiness identity 不一致。")
        return self


class EvolutionStableRollbackReadinessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRollbackReadinessService:
    """Verify that the active stable candidate can return to its exact prior slot."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_inspector: EvolutionStablePopulationCompletionInspectionPort,
        deployment_inspector: EvolutionStableDeploymentInspectionPort,
        release_slot_store: ReleaseSlotStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not isinstance(
            completion_inspector,
            EvolutionStablePopulationCompletionInspectionPort,
        ):
            raise TypeError("Stable Rollback Readiness 需要 5f5w inspection port。")
        if not isinstance(
            deployment_inspector,
            EvolutionStableDeploymentInspectionPort,
        ):
            raise TypeError("Stable Rollback Readiness 需要 Stable Deployment port。")
        if not isinstance(release_slot_store, ReleaseSlotStore):
            raise TypeError("Stable Rollback Readiness 需要 Release Slot Store。")
        self.completion_inspector = completion_inspector
        self.deployment_inspector = deployment_inspector
        self.release_slot_store = release_slot_store

    async def inspect(
        self,
        *,
        completion_receipt_id: str,
        intent_id: str,
    ) -> EvolutionStableRollbackReadiness:
        completion_id = _completion_id(completion_receipt_id)
        stable_intent_id = _intent_id(intent_id)
        try:
            completion_view, deployment_view = await asyncio.gather(
                self.completion_inspector.inspect(receipt_id=completion_id),
                self.deployment_inspector.inspect_stable_deployment(
                    intent_id=stable_intent_id
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionStableRollbackReadinessError(
                _safe_code(getattr(exc, "code", "stable_rollback_source_unavailable")),
                "Stable Rollback Readiness 缺少 current Completion 或 Deployment。",
            ) from exc
        completion = completion_view.receipt
        if not completion_view.stable_population_completion_authority:
            raise EvolutionStableRollbackReadinessError(
                "stable_rollback_completion_not_current",
                "Stable Population Completion 当前没有 authority。",
            )
        try:
            member_index = completion.intent_ids.index(stable_intent_id)
        except ValueError as exc:
            raise EvolutionStableRollbackReadinessError(
                "stable_rollback_intent_not_in_completion",
                "Stable Deployment Intent 不属于该 Population Completion。",
            ) from exc
        member_id = completion.installation_member_ids[member_index]
        deployment = deployment_view.receipt
        intent = deployment.preparation.intent
        pointer = deployment.activated_pointer
        if not (
            deployment_view.active_deployment_authority
            and deployment.workspace_root == str(self.workspace_root)
            and intent.intent_id == stable_intent_id
            and intent.proof.installation_credential.payload.member_id == member_id
            and intent.population_snapshot_id == completion.population_snapshot_id
            and intent.population_snapshot_sha256
            == completion.population_snapshot_sha256
            and intent.plan.plan_id == completion.plan_id
            and intent.plan.plan_sha256 == completion.plan_sha256
            and intent.candidate_version == completion.candidate_version
            and intent.installation_target == completion.candidate_target
            and pointer.current_slot_id == intent.candidate_slot_id
            and pointer.current_slot_sha256 == intent.candidate_slot_sha256
        ):
            raise EvolutionStableRollbackReadinessError(
                "stable_rollback_deployment_mismatch",
                "Stable Deployment 与 Population Completion identity 不一致。",
            )
        if (
            pointer.generation <= 1
            or pointer.previous_pointer_sha256 is None
            or pointer.previous_slot_id is None
            or pointer.previous_slot_sha256 is None
        ):
            raise EvolutionStableRollbackReadinessError(
                "stable_rollback_previous_slot_missing",
                "当前 Stable Deployment 没有可回滚 previous slot。",
            )
        try:
            active, prior = await asyncio.gather(
                asyncio.to_thread(self.release_slot_store.active),
                asyncio.to_thread(
                    self.release_slot_store.get_activation_event,
                    pointer.generation - 1,
                ),
            )
            if active != pointer or prior is None:
                raise EvolutionStableRollbackReadinessError(
                    "stable_rollback_pointer_changed",
                    "ARC-07 active pointer 或 prior activation 已变化。",
                )
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                prior.current_slot_id,
                prior.boot_receipt_id,
            )
        except EvolutionStableRollbackReadinessError:
            raise
        except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionStableRollbackReadinessError(
                _safe_code(getattr(exc, "code", "stable_rollback_slot_unavailable")),
                "Previous slot 或 Boot Receipt 当前不可验证。",
            ) from exc
        rollback_slot = resolved.slot
        rollback_boot = resolved.boot_receipt
        if not (
            prior.pointer_sha256 == pointer.previous_pointer_sha256
            and prior.generation + 1 == pointer.generation
            and prior.current_slot_id == pointer.previous_slot_id
            and prior.current_slot_sha256 == pointer.previous_slot_sha256
            and rollback_slot.slot_id == prior.current_slot_id
            and rollback_slot.slot_sha256 == prior.current_slot_sha256
            and rollback_boot.receipt_id == prior.boot_receipt_id
            and rollback_boot.receipt_sha256 == prior.boot_receipt_sha256
            and rollback_boot.slot_id == rollback_slot.slot_id
            and rollback_slot.slot_id != pointer.current_slot_id
        ):
            raise EvolutionStableRollbackReadinessError(
                "stable_rollback_lineage_mismatch",
                "Previous pointer、slot 与 Boot Receipt lineage 不一致。",
            )
        assessed_at = max(
            _aware(completion.completed_at),
            _aware(pointer.activated_at),
            _aware(rollback_boot.checked_at),
        ).isoformat()
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_STABLE_ROLLBACK_READINESS_POLICY,
            "workspace_root": str(self.workspace_root),
            "completion_receipt_id": completion.receipt_id,
            "completion_receipt_sha256": completion.receipt_sha256,
            "completion_source_set_sha256": completion.source_set_sha256,
            "population_snapshot_id": completion.population_snapshot_id,
            "population_snapshot_sha256": completion.population_snapshot_sha256,
            "installation_member_id": member_id,
            "stable_intent_id": stable_intent_id,
            "deployment_receipt_id": deployment.receipt_id,
            "deployment_receipt_sha256": deployment.receipt_sha256,
            "candidate_version": intent.candidate_version,
            "candidate_target": intent.installation_target,
            "candidate_slot_id": intent.candidate_slot_id,
            "candidate_slot_sha256": intent.candidate_slot_sha256,
            "candidate_manifest_sha256": intent.candidate_manifest_sha256,
            "expected_active_pointer_id": pointer.pointer_id,
            "expected_active_pointer_sha256": pointer.pointer_sha256,
            "expected_active_pointer_generation": pointer.generation,
            "rollback_pointer_id": prior.pointer_id,
            "rollback_pointer_sha256": prior.pointer_sha256,
            "rollback_pointer_generation": prior.generation,
            "rollback_slot_id": rollback_slot.slot_id,
            "rollback_slot_sha256": rollback_slot.slot_sha256,
            "rollback_manifest_sha256": rollback_slot.manifest_sha256,
            "rollback_boot_receipt_id": rollback_boot.receipt_id,
            "rollback_boot_receipt_sha256": rollback_boot.receipt_sha256,
            "rollback_binary_sha256": rollback_boot.binary_sha256,
            "completion_current": True,
            "active_deployment_current": True,
            "activation_chain_current": True,
            "previous_slot_retained": True,
            "previous_slot_bootable": True,
            "expected_pointer_cas_ready": True,
            "binary_rollback_readiness_authority": True,
            "config_data_rollback_readiness_authority": False,
            "stable_rollout_authority": False,
            "promotion_authority": False,
            "assessed_at": assessed_at,
        }
        digest = _digest(core)
        return EvolutionStableRollbackReadiness.model_validate(
            {
                **core,
                "readiness_id": f"evstablerollbackready_{digest[:24]}",
                "readiness_sha256": digest,
            }
        )


def render_stable_rollback_readiness(
    readiness: EvolutionStableRollbackReadiness,
) -> str:
    return "\n".join(
        (
            "## Stable Rollback Readiness",
            "",
            "- 状态：**Binary rollback ready**",
            f"- Readiness：`{readiness.readiness_id}`",
            f"- Completion：`{readiness.completion_receipt_id}`",
            f"- 当前 Pointer：`{readiness.expected_active_pointer_id}` "
            f"（generation {readiness.expected_active_pointer_generation}）",
            f"- 回滚 Slot：`{readiness.rollback_slot_id}`",
            f"- 回滚 Boot Receipt：`{readiness.rollback_boot_receipt_id}`",
            "- Config/Data rollback authority：`false`",
            "- Stable rollout authority：`false`",
            "- Promotion authority：`false`",
        )
    )


def _completion_id(value: str) -> str:
    normalized = str(value).strip()
    if not _COMPLETION_RE.fullmatch(normalized):
        raise ValueError(
            "completion_receipt_id 必须是 evstablepopcomplete_ 加 24 位小写十六进制。"
        )
    return normalized


def _intent_id(value: str) -> str:
    normalized = str(value).strip()
    if not _INTENT_RE.fullmatch(normalized):
        raise ValueError("intent_id 必须是 evrestableintent_ 加 24 位小写十六进制。")
    return normalized


def _safe_code(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().lower())
    return normalized[:128] or "stable_rollback_source_unavailable"


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_ROLLBACK_READINESS_POLICY",
    "EvolutionStableDeploymentInspectionPort",
    "EvolutionStablePopulationCompletionInspectionPort",
    "EvolutionStableRollbackReadiness",
    "EvolutionStableRollbackReadinessError",
    "EvolutionStableRollbackReadinessService",
    "render_stable_rollback_readiness",
]
