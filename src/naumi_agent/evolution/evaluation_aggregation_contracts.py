"""Durable authority describing every lane required by final Evolution evaluation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.adversarial_batch_requests import (
    AdversarialBatchPlatform,
    EvolutionAdversarialBatchRequest,
)

EVALUATION_AGGREGATION_CONTRACT_POLICY = "evolution-evaluation-aggregation-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionEvaluationAggregationLane(_StrictModel):
    order: int = Field(ge=1, le=3)
    platform: AdversarialBatchPlatform
    red_lane_order: int = Field(ge=1, le=6)
    red_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    green_lane_order: int = Field(ge=1, le=6)
    green_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EvolutionEvaluationAggregationContract(_StrictModel):
    """A non-final, tamper-evident declaration of complete evaluation coverage."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-evaluation-aggregation-v1"] = (
        EVALUATION_AGGREGATION_CONTRACT_POLICY
    )
    contract_id: str = Field(pattern=r"^evagg_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    request_id: str = Field(pattern=r"^evadvreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    requested_samples: int = Field(ge=5, le=100)
    required_platforms: tuple[AdversarialBatchPlatform, ...] = Field(
        min_length=1,
        max_length=3,
    )
    adversarial_lanes: tuple[EvolutionEvaluationAggregationLane, ...] = Field(
        min_length=1,
        max_length=3,
    )
    batch_request: EvolutionAdversarialBatchRequest
    interventional_lane_required: Literal[True] = True
    exact_platform_coverage_required: Literal[True] = True
    candidate_evaluation_complete: Literal[False] = False
    final_receipt_issued: Literal[False] = False

    @field_validator("workspace_root")
    @classmethod
    def _absolute_workspace(cls, value: str) -> str:
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Evaluation Aggregation workspace 含非法字符。")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("Evaluation Aggregation workspace 必须是绝对路径。")
        return str(path.resolve())

    @model_validator(mode="after")
    def _authority_is_exact_and_tamper_evident(self) -> Self:
        request = self.batch_request
        if not (
            self.request_id == request.request_id
            and self.request_sha256 == request.request_sha256
            and self.validation_plan_id == request.validation_plan_id
            and self.validation_plan_sha256 == request.validation_plan_sha256
            and self.candidate_id == request.candidate_id
            and self.candidate_revision == request.candidate_revision
            and self.suite_id == request.suite_id
            and self.requested_samples == request.requested_samples
            and self.required_platforms == request.required_platforms
        ):
            raise ValueError("Evaluation Aggregation projection 与 Batch Request 不一致。")
        expected = _aggregation_lanes(request)
        if self.adversarial_lanes != expected:
            raise ValueError("Evaluation Aggregation lanes 未精确覆盖 Batch Request。")
        payload = self.model_dump(
            mode="json",
            exclude={"contract_id", "contract_sha256"},
        )
        digest = _sha256_payload(payload)
        if not hmac.compare_digest(self.contract_sha256, digest):
            raise ValueError("Evaluation Aggregation Contract 摘要不一致。")
        if self.contract_id != f"evagg_{digest[:24]}":
            raise ValueError("Evaluation Aggregation Contract identity 不一致。")
        return self


class EvolutionEvaluationAggregationContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionEvaluationAggregationContractBuilder:
    def build(
        self,
        *,
        workspace_root: str | Path,
        batch_request: EvolutionAdversarialBatchRequest,
    ) -> EvolutionEvaluationAggregationContract:
        try:
            request = EvolutionAdversarialBatchRequest.model_validate(
                batch_request.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_request_invalid",
                "Evaluation Aggregation Batch Request 无效或已被篡改。",
            ) from exc
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_workspace_missing",
                "Evaluation Aggregation workspace 不存在。",
            ) from exc
        if not workspace.is_dir():
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_workspace_invalid",
                "Evaluation Aggregation workspace 不是目录。",
            )
        _verify_workspace_source(workspace, request)
        payload = {
            "schema_version": 1,
            "policy_version": EVALUATION_AGGREGATION_CONTRACT_POLICY,
            "workspace_root": str(workspace),
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "validation_plan_id": request.validation_plan_id,
            "validation_plan_sha256": request.validation_plan_sha256,
            "candidate_id": request.candidate_id,
            "candidate_revision": request.candidate_revision,
            "suite_id": request.suite_id,
            "requested_samples": request.requested_samples,
            "required_platforms": list(request.required_platforms),
            "adversarial_lanes": [
                item.model_dump(mode="json") for item in _aggregation_lanes(request)
            ],
            "batch_request": request.model_dump(mode="json"),
            "interventional_lane_required": True,
            "exact_platform_coverage_required": True,
            "candidate_evaluation_complete": False,
            "final_receipt_issued": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionEvaluationAggregationContract.model_validate({
            **payload,
            "contract_id": f"evagg_{digest[:24]}",
            "contract_sha256": digest,
        })


class EvolutionEvaluationAggregationContractStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        contract: EvolutionEvaluationAggregationContract,
    ) -> EvolutionEvaluationAggregationContract:
        artifact = EvolutionEvaluationAggregationContract.model_validate(
            contract.model_dump(mode="json")
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_evaluation_aggregation_contracts "
                        "WHERE contract_id = ?",
                        (artifact.contract_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != artifact:
                        await db.rollback()
                        raise EvolutionEvaluationAggregationContractError(
                            "evaluation_aggregation_store_conflict",
                            "同一 Aggregation Contract 不可覆盖为不同内容。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_evaluation_aggregation_contracts "
                    "(contract_id, request_id, contract_sha256, contract_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        artifact.contract_id,
                        artifact.request_id,
                        artifact.contract_sha256,
                        _json_dumps(artifact.model_dump(mode="json")),
                    ),
                )
                await db.commit()
        except EvolutionEvaluationAggregationContractError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_store_error",
                "Evaluation Aggregation Contract 无法持久化。",
            ) from exc
        restored = await self.get(artifact.contract_id)
        assert restored is not None
        return restored

    async def get(
        self,
        contract_id: str,
    ) -> EvolutionEvaluationAggregationContract | None:
        if not isinstance(contract_id, str) or re.fullmatch(
            r"evagg_[0-9a-f]{24}", contract_id
        ) is None:
            raise ValueError("Aggregation contract_id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_evaluation_aggregation_contracts "
                        "WHERE contract_id = ?",
                        (contract_id,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_store_corrupt",
                "Evaluation Aggregation Contract 损坏或无法读取。",
            ) from exc


class EvolutionEvaluationAggregationContractIssuer:
    def __init__(
        self,
        *,
        store: EvolutionEvaluationAggregationContractStore,
        builder: EvolutionEvaluationAggregationContractBuilder | None = None,
    ) -> None:
        if not isinstance(store, EvolutionEvaluationAggregationContractStore):
            raise TypeError("Evaluation Aggregation issuer 需要 Contract Store。")
        self._store = store
        self._builder = builder or EvolutionEvaluationAggregationContractBuilder()

    async def issue(
        self,
        *,
        workspace_root: str | Path,
        batch_request: EvolutionAdversarialBatchRequest,
    ) -> EvolutionEvaluationAggregationContract:
        artifact = self._builder.build(
            workspace_root=workspace_root,
            batch_request=batch_request,
        )
        return await self._store.record(artifact)


def render_evaluation_aggregation_contract(
    contract: EvolutionEvaluationAggregationContract,
) -> str:
    artifact = EvolutionEvaluationAggregationContract.model_validate(
        contract.model_dump(mode="json")
    )
    lines = [
        f"# Evaluation Aggregation Contract `{artifact.contract_id}`",
        "",
        "**这是最终评测的覆盖合同，不是候选最终 Evaluation Receipt。**",
        "",
        f"- Candidate：`{artifact.candidate_id}` · revision {artifact.candidate_revision}",
        f"- Validation Plan：`{artifact.validation_plan_id}`",
        f"- Adversarial Request：`{artifact.request_id}`",
        f"- Suite / samples：`{artifact.suite_id}` / {artifact.requested_samples}",
        f"- 必需平台：{', '.join(f'`{item}`' for item in artifact.required_platforms)}",
        "- Interventional lane：必须提供 1 个",
        "",
        "## Adversarial RED/GREEN pairs",
        "",
    ]
    lines.extend(
        f"- `{lane.platform}`：`{lane.red_batch_id}` → `{lane.green_batch_id}`"
        for lane in artifact.adversarial_lanes
    )
    lines.extend((
        "",
        f"- Contract SHA-256：`{artifact.contract_sha256}`",
        "",
        "下一步：收齐并重验合同声明的全部 lane 后，签发最终 Evaluation Receipt。",
    ))
    return "\n".join(lines)


def _aggregation_lanes(
    request: EvolutionAdversarialBatchRequest,
) -> tuple[EvolutionEvaluationAggregationLane, ...]:
    lanes = []
    for order, platform in enumerate(request.required_platforms, start=1):
        pairs = tuple(item for item in request.lanes if item.platform == platform)
        if len(pairs) != 2 or tuple(item.phase for item in pairs) != ("red", "green"):
            raise ValueError("Adversarial Batch Request platform lane pair 不完整。")
        red, green = pairs
        lanes.append(EvolutionEvaluationAggregationLane(
            order=order,
            platform=platform,
            red_lane_order=red.order,
            red_batch_id=red.batch_id,
            green_lane_order=green.order,
            green_batch_id=green.batch_id,
        ))
    return tuple(lanes)


def _verify_workspace_source(
    workspace: Path,
    request: EvolutionAdversarialBatchRequest,
) -> None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def run(*args: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(workspace), *args],
                check=False,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_source_unreadable",
                "无法读取 Evaluation Aggregation workspace Git authority。",
            ) from exc
        if completed.returncode != 0:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_source_mismatch",
                "Batch Request baseline 不属于当前 workspace Git authority。",
            )
        if len(completed.stdout) > 64 * 1024 * 1024:
            raise EvolutionEvaluationAggregationContractError(
                "evaluation_aggregation_source_oversized",
                "Evaluation Aggregation Git tree listing 超过 64 MiB。",
            )
        return completed.stdout

    try:
        top = Path(run("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    except (UnicodeError, OSError) as exc:
        raise EvolutionEvaluationAggregationContractError(
            "evaluation_aggregation_source_unreadable",
            "无法解析 Evaluation Aggregation workspace Git root。",
        ) from exc
    if top != workspace:
        raise EvolutionEvaluationAggregationContractError(
            "evaluation_aggregation_workspace_not_root",
            "Evaluation Aggregation workspace 必须是精确 Git 仓库根目录。",
        )
    run("cat-file", "-e", f"{request.baseline_commit}^{{commit}}")
    tree_listing = run(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        request.baseline_commit,
    )
    if not hmac.compare_digest(
        hashlib.sha256(tree_listing).hexdigest(),
        request.baseline_tree_sha256,
    ):
        raise EvolutionEvaluationAggregationContractError(
            "evaluation_aggregation_source_mismatch",
            "Batch Request baseline tree 与当前 workspace Git object 不一致。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_evaluation_aggregation_contracts (
            contract_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL,
            contract_json TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_evolution_evaluation_aggregation_request "
        "ON evolution_evaluation_aggregation_contracts(request_id, contract_id)"
    )


def _from_row(row: aiosqlite.Row) -> EvolutionEvaluationAggregationContract:
    artifact = EvolutionEvaluationAggregationContract.model_validate_json(
        str(row["contract_json"])
    )
    if not (
        row["contract_id"] == artifact.contract_id
        and row["request_id"] == artifact.request_id
        and row["contract_sha256"] == artifact.contract_sha256
    ):
        raise ValueError("Evaluation Aggregation Store row 与 payload 不一致。")
    return artifact


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _json_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "EVALUATION_AGGREGATION_CONTRACT_POLICY",
    "EvolutionEvaluationAggregationContract",
    "EvolutionEvaluationAggregationContractBuilder",
    "EvolutionEvaluationAggregationContractError",
    "EvolutionEvaluationAggregationContractIssuer",
    "EvolutionEvaluationAggregationContractStore",
    "EvolutionEvaluationAggregationLane",
    "render_evaluation_aggregation_contract",
]
