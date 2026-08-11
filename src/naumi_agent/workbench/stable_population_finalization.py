"""Typed Workbench projection for Stable Population finalization authority."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationView,
)

StablePopulationFinalizationInvalidationReason = Literal[
    "member_finalization_authority_changed",
    "member_receipt_set_changed",
    "newer_population_finalization_exists",
    "population_finalization_receipt_changed",
    "population_snapshot_not_current",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class WorkbenchStablePopulationFinalizationProjection(_StrictModel):
    """Bounded read-only projection consumed identically by terminal frontends."""

    schema_version: Literal[1] = 1
    status: Literal["pending", "completed", "revoked"]
    receipt_id: str = Field(default="", pattern=r"^(?:|evstableremotepopfinal_[0-9a-f]{24})$")
    receipt_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    population_snapshot_id: str = Field(default="", pattern=r"^(?:|relpopsnapshot_[0-9a-f]{24})$")
    population_snapshot_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    candidate_version: str = Field(default="", max_length=128)
    completed_members: int = Field(ge=0, le=10_000)
    population_denominator: int = Field(ge=0, le=10_000)
    finalized_at: str = Field(default="", max_length=100)
    historical_fact: bool
    current_authority: bool
    invalidation_reasons: tuple[StablePopulationFinalizationInvalidationReason, ...] = Field(
        max_length=5
    )
    config_data_finalization_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        identifiers = (
            self.receipt_id,
            self.receipt_sha256,
            self.population_snapshot_id,
            self.population_snapshot_sha256,
            self.candidate_version,
            self.finalized_at,
        )
        reasons = tuple(sorted(set(self.invalidation_reasons)))
        if self.invalidation_reasons != reasons:
            raise ValueError("Stable Population finalization 撤权原因无效。")
        if self.status == "pending":
            valid = bool(
                not any(identifiers)
                and self.completed_members == 0
                and self.population_denominator == 0
                and not self.historical_fact
                and not self.current_authority
                and not self.invalidation_reasons
            )
        else:
            try:
                finalized_at = datetime.fromisoformat(self.finalized_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Stable Population finalization 完成时间无效。") from exc
            valid = bool(
                all(identifiers)
                and finalized_at.utcoffset() is not None
                and self.completed_members == self.population_denominator
                and self.population_denominator > 0
                and self.historical_fact
                and self.current_authority is (self.status == "completed")
                and bool(self.invalidation_reasons) is (self.status == "revoked")
            )
        if not valid:
            raise ValueError("Stable Population finalization projection 状态无效。")
        return self


class StablePopulationFinalizationSource(Protocol):
    async def latest_view(
        self,
    ) -> EvolutionStableRemotePopulationFinalizationView | None: ...


class StablePopulationFinalizationWorkbenchReader:
    """Adapt Evolution authority to a terminal-safe Workbench projection."""

    def __init__(self, source: StablePopulationFinalizationSource) -> None:
        if not callable(getattr(source, "latest_view", None)):
            raise TypeError("Stable Population finalization source 必须实现 latest_view()。")
        self.source = source

    async def project(self) -> WorkbenchStablePopulationFinalizationProjection:
        view = await self.source.latest_view()
        if view is None:
            return WorkbenchStablePopulationFinalizationProjection(
                status="pending",
                completed_members=0,
                population_denominator=0,
                historical_fact=False,
                current_authority=False,
                invalidation_reasons=(),
            )
        checked = EvolutionStableRemotePopulationFinalizationView.model_validate_json(
            view.model_dump_json()
        )
        receipt = checked.receipt
        return WorkbenchStablePopulationFinalizationProjection(
            status=("completed" if checked.stable_population_finalization_authority else "revoked"),
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.receipt_sha256,
            population_snapshot_id=receipt.population_snapshot_id,
            population_snapshot_sha256=receipt.population_snapshot_sha256,
            candidate_version=receipt.candidate_version,
            completed_members=len(receipt.members),
            population_denominator=receipt.population_denominator,
            finalized_at=receipt.finalized_at,
            historical_fact=checked.stable_population_finalization_fact,
            current_authority=checked.stable_population_finalization_authority,
            invalidation_reasons=checked.invalidation_reasons,
        )


__all__ = [
    "StablePopulationFinalizationInvalidationReason",
    "StablePopulationFinalizationSource",
    "StablePopulationFinalizationWorkbenchReader",
    "WorkbenchStablePopulationFinalizationProjection",
]
