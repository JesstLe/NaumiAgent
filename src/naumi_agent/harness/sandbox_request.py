"""Immutable authority requests for native Harness Sandbox Eval batches."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.models import HarnessCheckSpec, HarnessProfile

SANDBOX_EVAL_REQUEST_POLICY = "harness-sandbox-eval-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HarnessSandboxEvalRequestCheck(_StrictModel):
    order: int = Field(ge=1, le=80)
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    spec_sha256: str = Field(pattern=_SHA256_RE)
    argv_sha256: str = Field(pattern=_SHA256_RE)
    timeout_seconds: int = Field(ge=1, le=3_600)


class HarnessSandboxEvalRequest(_StrictModel):
    """Tamper-evident request compiled before permission or Worker admission."""

    schema_version: Literal[1] = 1
    policy_version: Literal["harness-sandbox-eval-request-v1"] = (
        SANDBOX_EVAL_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^hseval_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    suite_id: str = Field(pattern=r"^harness_sandbox_[0-9a-f]{24}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    lane: Literal["sandbox"] = "sandbox"
    requested_samples: int = Field(ge=5, le=100)
    source_revision: str = Field(pattern=_GIT_OBJECT_RE)
    source_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    checks: tuple[HarnessSandboxEvalRequestCheck, ...] = Field(
        min_length=1,
        max_length=80,
    )
    check_timeout_seconds_per_sample: int = Field(ge=1, le=288_000)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    source_materialization: Literal["arc04_ephemeral_git_revision"] = (
        "arc04_ephemeral_git_revision"
    )
    source_clean: Literal[True] = True
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    runtime_identity_required: Literal[True] = True
    profile_trust_revalidation_required: Literal[True] = True
    continuous_sample_indexes_required: Literal[True] = True
    harness_result_store_required: Literal[True] = True
    execution_authority_required: Literal[True] = True
    request_ready: Literal[True] = True

    @field_validator("workspace_root")
    @classmethod
    def _workspace_is_absolute(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or str(path.resolve(strict=False)) != value:
            raise ValueError("Sandbox Eval workspace_root 必须是规范绝对路径。")
        return value

    @model_validator(mode="after")
    def _request_is_bounded_and_tamper_evident(self) -> Self:
        if tuple(item.order for item in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Sandbox Eval checks 必须按连续顺序排列。")
        check_ids = tuple(item.check_id for item in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Sandbox Eval checks 不得重复。")
        timeout = sum(item.timeout_seconds for item in self.checks)
        if self.check_timeout_seconds_per_sample != timeout:
            raise ValueError("Sandbox Eval 单样本 timeout 汇总不一致。")
        if timeout * self.requested_samples > self.max_total_duration_seconds:
            raise ValueError("Sandbox Eval checks 的最坏耗时超过 Batch 总预算。")
        if self.suite_id != _suite_id(
            self.checks,
            max_total_duration_seconds=self.max_total_duration_seconds,
        ):
            raise ValueError("Sandbox Eval suite identity 与执行过程不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, expected):
            raise ValueError("Sandbox Eval Request 摘要不一致。")
        if self.request_id != f"hseval_{expected[:24]}":
            raise ValueError("Sandbox Eval Request identity 不一致。")
        return self

    @property
    def authority_key(self) -> str:
        return self.request_sha256


class HarnessSandboxEvalRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessSandboxEvalRequestBuilder:
    """Compile trusted Profile checks and one clean Git HEAD into authority."""

    def build(
        self,
        *,
        workspace_root: str | Path,
        profile: HarnessProfile,
        profile_digest: str,
        profile_trusted: bool,
        check_ids: tuple[str, ...],
        batch_id: str,
        requested_samples: int = 5,
    ) -> HarnessSandboxEvalRequest:
        if not isinstance(profile, HarnessProfile):
            raise TypeError("Sandbox Eval Request 需要 HarnessProfile。")
        if profile_trusted is not True:
            raise self._error(
                "profile_untrusted",
                "Harness Profile 未受信任，不能编译 Sandbox Eval Request。",
            )
        if (
            not isinstance(profile_digest, str)
            or re.fullmatch(_SHA256_RE, profile_digest) is None
        ):
            raise self._error(
                "profile_digest_invalid",
                "Harness Profile digest 必须是 SHA-256。",
            )
        if (
            not isinstance(check_ids, tuple)
            or not 1 <= len(check_ids) <= 80
            or any(
                not isinstance(item, str) or _CHECK_ID_RE.fullmatch(item) is None
                for item in check_ids
            )
        ):
            raise self._error(
                "check_ids_invalid",
                "Sandbox Eval check_ids 必须包含 1..80 个有效 Profile check id。",
            )
        if len(check_ids) != len(set(check_ids)):
            raise self._error(
                "check_ids_duplicated",
                "Sandbox Eval check_ids 不得重复。",
            )
        if not isinstance(batch_id, str) or _BATCH_ID_RE.fullmatch(batch_id) is None:
            raise self._error(
                "batch_id_invalid",
                "Sandbox Eval batch_id 格式无效。",
            )
        if (
            isinstance(requested_samples, bool)
            or not isinstance(requested_samples, int)
            or not 5 <= requested_samples <= 100
        ):
            raise self._error(
                "sample_count_invalid",
                "Sandbox Eval 样本数必须在 5..100。",
            )

        by_id = {item.id: item for item in profile.checks}
        missing = tuple(item for item in check_ids if item not in by_id)
        if missing:
            raise self._error(
                "profile_check_missing",
                f"Harness Profile 未声明检查：{', '.join(missing)}。",
            )
        checks = tuple(
            _request_check(order, by_id[check_id])
            for order, check_id in enumerate(check_ids, start=1)
        )
        timeout = sum(item.timeout_seconds for item in checks)
        max_total_duration = min(profile.evals.max_duration_seconds, 3_600)
        if max_total_duration < 60:
            raise self._error(
                "duration_budget_invalid",
                "Harness Profile 的 Eval 总时限低于 Sandbox Batch 最小值 60 秒。",
            )
        if timeout * requested_samples > max_total_duration:
            raise self._error(
                "duration_budget_exceeded",
                "Sandbox Eval checks 的最坏耗时超过 Profile Eval 总预算。",
            )

        if not isinstance(workspace_root, (str, Path)):
            raise TypeError("workspace_root 必须是字符串或 Path。")
        workspace, revision, tree_sha256 = capture_clean_revision(workspace_root)
        suite_id = _suite_id(
            checks,
            max_total_duration_seconds=max_total_duration,
        )
        payload = {
            "schema_version": 1,
            "policy_version": SANDBOX_EVAL_REQUEST_POLICY,
            "workspace_root": str(workspace),
            "suite_id": suite_id,
            "batch_id": batch_id,
            "lane": "sandbox",
            "requested_samples": requested_samples,
            "source_revision": revision,
            "source_tree_sha256": tree_sha256,
            "profile_sha256": profile_digest,
            "checks": [item.model_dump(mode="json") for item in checks],
            "check_timeout_seconds_per_sample": timeout,
            "max_total_duration_seconds": max_total_duration,
            "source_materialization": "arc04_ephemeral_git_revision",
            "source_clean": True,
            "network_access": False,
            "dependency_installation": False,
            "runtime_identity_required": True,
            "profile_trust_revalidation_required": True,
            "continuous_sample_indexes_required": True,
            "harness_result_store_required": True,
            "execution_authority_required": True,
            "request_ready": True,
        }
        digest = _sha256_payload(payload)
        return HarnessSandboxEvalRequest.model_validate({
            **payload,
            "request_id": f"hseval_{digest[:24]}",
            "request_sha256": digest,
        })

    @staticmethod
    def _error(code: str, message: str) -> HarnessSandboxEvalRequestError:
        return HarnessSandboxEvalRequestError(f"sandbox_request_{code}", message)


def validate_request_checks(
    request: HarnessSandboxEvalRequest,
    profile: HarnessProfile,
) -> tuple[HarnessCheckSpec, ...]:
    """Resolve current Profile specs only when every immutable digest still matches."""
    if not isinstance(request, HarnessSandboxEvalRequest):
        raise TypeError("request 必须是 HarnessSandboxEvalRequest。")
    if not isinstance(profile, HarnessProfile):
        raise TypeError("profile 必须是 HarnessProfile。")
    by_id = {item.id: item for item in profile.checks}
    resolved: list[HarnessCheckSpec] = []
    for expected in request.checks:
        check = by_id.get(expected.check_id)
        if (
            check is None
            or _check_spec_sha256(check) != expected.spec_sha256
            or _sha256_payload(list(check.argv)) != expected.argv_sha256
            or check.timeout_seconds != expected.timeout_seconds
        ):
            raise HarnessSandboxEvalRequestError(
                "sandbox_request_profile_check_drifted",
                f"Sandbox Eval check {expected.check_id} 已偏离请求 authority。",
            )
        resolved.append(check)
    return tuple(resolved)


def _request_check(
    order: int,
    check: HarnessCheckSpec,
) -> HarnessSandboxEvalRequestCheck:
    return HarnessSandboxEvalRequestCheck(
        order=order,
        check_id=check.id,
        spec_sha256=_check_spec_sha256(check),
        argv_sha256=_sha256_payload(list(check.argv)),
        timeout_seconds=check.timeout_seconds,
    )


def _check_spec_sha256(check: HarnessCheckSpec) -> str:
    return _sha256_payload(check.model_dump(mode="json"))


def _suite_id(
    checks: tuple[HarnessSandboxEvalRequestCheck, ...],
    *,
    max_total_duration_seconds: int,
) -> str:
    digest = _sha256_payload({
        "policy_version": SANDBOX_EVAL_REQUEST_POLICY,
        "checks": [item.model_dump(mode="json") for item in checks],
        "max_total_duration_seconds": max_total_duration_seconds,
        "network_access": False,
        "dependency_installation": False,
    })
    return f"harness_sandbox_{digest[:24]}"


def capture_clean_revision(
    workspace_root: str | Path,
) -> tuple[Path, str, str]:
    try:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_workspace_unavailable",
            "Sandbox Eval 工作区不存在或无法读取。",
        ) from exc
    if not workspace.is_dir():
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_workspace_unavailable",
            "Sandbox Eval 工作区不是目录。",
        )
    top = _git_text(workspace, "rev-parse", "--show-toplevel")
    if Path(top).resolve(strict=True) != workspace:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_root_mismatch",
            "Sandbox Eval 必须从精确 Git 仓库根目录编译。",
        )
    status_before = _git_bytes(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status_before:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_worktree_dirty",
            "Sandbox Eval v1 只接受干净工作树；请先提交、暂存到独立候选快照或清理改动。",
        )
    revision = _git_text(
        workspace,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    ).lower()
    if re.fullmatch(_GIT_OBJECT_RE, revision) is None:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_revision_invalid",
            "Sandbox Eval 无法取得完整 Git commit identity。",
        )
    tree_listing = _git_bytes(
        workspace,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
    )
    status_after = _git_bytes(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    revision_after = _git_text(
        workspace,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    ).lower()
    if status_after or revision_after != revision:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_source_drifted",
            "Sandbox Eval 源码在请求编译期间发生变化。",
        )
    return workspace, revision, hashlib.sha256(tree_listing).hexdigest()


def _git_text(workspace: Path, *args: str) -> str:
    raw = _git_bytes(workspace, *args)
    try:
        value = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_output_invalid",
            "Git identity 输出不是有效 UTF-8。",
        ) from exc
    if not value:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_output_empty",
            "Git identity 输出为空。",
        )
    return value


def _git_bytes(workspace: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=False,
            capture_output=True,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_unavailable",
            "Git identity 命令不可用或超时。",
        ) from exc
    if completed.returncode != 0:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_failed",
            "Git identity 命令执行失败。",
        )
    if len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES:
        raise HarnessSandboxEvalRequestError(
            "sandbox_request_git_output_too_large",
            "Git identity 输出超过 16 MiB 上限。",
        )
    return completed.stdout


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "HarnessSandboxEvalRequest",
    "HarnessSandboxEvalRequestBuilder",
    "HarnessSandboxEvalRequestCheck",
    "HarnessSandboxEvalRequestError",
    "SANDBOX_EVAL_REQUEST_POLICY",
    "capture_clean_revision",
    "validate_request_checks",
]
