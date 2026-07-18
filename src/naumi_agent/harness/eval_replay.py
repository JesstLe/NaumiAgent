"""HAR-08 adapter turning HAR-05 safe replay into comparable Eval evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalGuardrailResult,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.replay_models import HarnessReplayLookup, HarnessReplayResult

SAFE_REPLAY_EVAL_RUNNER_VERSION = "safe_replay@1"


def build_safe_replay_eval_result(
    lookup: HarnessReplayLookup,
    *,
    workspace_root: str | Path,
    profile_digest: str | None,
    profile_trusted: bool,
    source_before: HarnessEvalSourceIdentity | None,
    source_after: HarnessEvalSourceIdentity | None,
    source_code: str = "",
    duration_ms: float = 0,
) -> HarnessEvalSuiteResult:
    """Build one deterministic, no-execution Eval suite from a replay lookup."""
    if lookup.status != "ok" or lookup.result is None:
        return _unavailable_result(
            lookup,
            profile_digest=profile_digest,
            source_code=source_code,
            duration_ms=duration_ms,
        )
    replay = lookup.result
    policy = HarnessEvalComparisonPolicy()
    suite_payload = {
        "schema_version": 1,
        "runner_version": SAFE_REPLAY_EVAL_RUNNER_VERSION,
        "run_id": replay.run_id,
        "baseline_manifest_sha256": replay.baseline_manifest_sha256,
        "baseline_rule_version": replay.baseline_rule_version,
        "baseline_explanation_sha256": replay.baseline_explanation_sha256,
        "expected_status": "reproduced",
    }
    suite_sha256 = _sha256_payload(suite_payload)
    suite_id = f"replay_{hashlib.sha256(replay.run_id.encode()).hexdigest()[:16]}"
    case = _replay_case(replay)
    result = HarnessEvalSuiteResult(
        suite_id=suite_id,
        title="Harness Safe Replay 回归评测",
        suite_path=f"harness-replay:{replay.run_id}",
        suite_sha256=suite_sha256,
        status=(
            EvalRunStatus.PASSED
            if case.status is EvalCaseStatus.PASSED
            else EvalRunStatus.FAILED
        ),
        cases=(case,),
        code=case.code,
        message=case.message if case.status is not EvalCaseStatus.PASSED else "",
        comparison_policy=policy,
        duration_ms=duration_ms,
    )
    if profile_digest is None:
        return result.model_copy(
            update={"baseline_identity_code": source_code or "profile_unavailable"}
        )
    if source_code:
        return result.model_copy(update={"baseline_identity_code": source_code})
    if source_before is None or source_after is None:
        return result.model_copy(
            update={"baseline_identity_code": "baseline_source_unavailable"}
        )
    if source_before != source_after:
        return result.model_copy(
            update={"baseline_identity_code": "baseline_source_changed"}
        )
    try:
        configuration = HarnessEvalConfigurationIdentity.create(
            suite_id=suite_id,
            suite_sha256=suite_sha256,
            profile_sha256=profile_digest,
            policy_sha256=policy.sha256,
            runner_version=SAFE_REPLAY_EVAL_RUNNER_VERSION,
            repetitions=1,
            live=False,
        )
        identity = build_eval_baseline_identity(
            workspace_root,
            configuration=configuration,
            profile_trusted=profile_trusted,
            source_identity=source_before,
        )
    except (OSError, ValidationError, ValueError):
        return result.model_copy(
            update={"baseline_identity_code": "baseline_configuration_invalid"}
        )
    return result.model_copy(update={"baseline_identity": identity})


def _replay_case(replay: HarnessReplayResult) -> HarnessEvalCaseResult:
    if replay.status == "reproduced":
        status = EvalCaseStatus.PASSED
        code = ""
        message = "Replay manifest、解释规则与 artifact 摘要均已复现。"
    elif replay.status == "changed":
        status = EvalCaseStatus.IMPLEMENTATION_FAILURE
        code = "replay_behavior_changed"
        message = "Replay 行为与持久 baseline 不同；需要审查规则或实现变更。"
    elif replay.status == "partial":
        status = EvalCaseStatus.EVALUATION_ERROR
        code = "replay_evidence_partial"
        message = "Replay 证据不完整，不能形成产品回归结论。"
    else:
        status = EvalCaseStatus.EVALUATION_ERROR
        code = "replay_evidence_corrupt"
        message = "Replay baseline 或 artifact 完整性校验失败。"
    return HarnessEvalCaseResult(
        case_id="replay_integrity",
        runner=SAFE_REPLAY_EVAL_RUNNER_VERSION,
        status=status,
        primary_metric="replay_reproduced",
        guardrails=(
            HarnessEvalGuardrailResult(
                guardrail="no_model",
                status=EvalGuardrailStatus.PASSED,
            ),
            HarnessEvalGuardrailResult(
                guardrail="no_side_effect",
                status=EvalGuardrailStatus.PASSED,
            ),
        ),
        code=code,
        message=message,
    )


def _unavailable_result(
    lookup: HarnessReplayLookup,
    *,
    profile_digest: str | None,
    source_code: str,
    duration_ms: float,
) -> HarnessEvalSuiteResult:
    code = "replay_not_found" if lookup.status == "not_found" else "replay_unavailable"
    message = (
        "当前工作区没有可评测的 Harness 运行记录。"
        if lookup.status == "not_found"
        else (
            "Harness Replay 数据当前不可安全读取；若该运行尚无 baseline，"
            "请先执行 `/harness replay <run-id>`。"
        )
    )
    return HarnessEvalSuiteResult(
        suite_id="safe_replay",
        title="Harness Safe Replay 回归评测",
        suite_path="harness-replay:unavailable",
        status=EvalRunStatus.EVALUATION_ERROR,
        cases=(
            HarnessEvalCaseResult(
                case_id="replay_integrity",
                runner=SAFE_REPLAY_EVAL_RUNNER_VERSION,
                status=EvalCaseStatus.EVALUATION_ERROR,
                primary_metric="replay_reproduced",
                guardrails=(
                    HarnessEvalGuardrailResult(
                        guardrail="no_model",
                        status=EvalGuardrailStatus.PASSED,
                    ),
                    HarnessEvalGuardrailResult(
                        guardrail="no_side_effect",
                        status=EvalGuardrailStatus.PASSED,
                    ),
                ),
                code=code,
                message=message,
            ),
        ),
        code=code,
        message=message,
        baseline_identity_code=(
            source_code
            or ("profile_unavailable" if profile_digest is None else "replay_unavailable")
        ),
        duration_ms=duration_ms,
    )


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["SAFE_REPLAY_EVAL_RUNNER_VERSION", "build_safe_replay_eval_result"]
