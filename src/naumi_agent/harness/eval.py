"""Bounded, deterministic offline Harness evaluation runners."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_source_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCase,
    HarnessEvalCaseResult,
    HarnessEvalGuardrailResult,
    HarnessEvalReport,
    HarnessEvalSuite,
    HarnessEvalSuiteResult,
    HarnessProtocolActual,
)
from naumi_agent.harness.fingerprint import TreeFingerprintError
from naumi_agent.ui.protocol import (
    ProtocolNegotiationError,
    negotiate_hello,
    normalize_client_record,
)

MAX_SUITE_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 64 * 1024
PROTOCOL_HELLO_RUNNER_VERSION = "protocol_hello@1"


@dataclass(frozen=True, slots=True)
class _LoadedSuite:
    suite: HarnessEvalSuite
    sha256: str


class HarnessEvalAssetError(ValueError):
    """A stable, user-safe error in evaluation assets rather than product behavior."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evaluate_suite_file(
    workspace_root: str | Path,
    suite_path: str | Path,
    *,
    profile_digest: str | None = None,
    profile_trusted: bool = False,
) -> HarnessEvalSuiteResult:
    """Load and run one declared offline suite without commands, model, or writes."""
    workspace = Path(workspace_root).expanduser().resolve()
    source_before, source_code = _capture_baseline_source(
        workspace,
        enabled=profile_digest is not None,
    )
    result = _evaluate_suite_file_raw(workspace, suite_path)
    source_after, source_code = _capture_baseline_source_after(
        workspace,
        source_before,
        source_code,
    )
    return _attach_baseline_identity(
        result,
        workspace=workspace,
        profile_digest=profile_digest,
        profile_trusted=profile_trusted,
        source_before=source_before,
        source_after=source_after,
        source_code=source_code,
    )


def _evaluate_suite_file_raw(
    workspace_root: str | Path,
    suite_path: str | Path,
) -> HarnessEvalSuiteResult:
    """Evaluate one suite without baseline identity orchestration."""
    started = time.perf_counter()
    workspace = Path(workspace_root).expanduser().resolve()
    candidate = Path(suite_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    display = _display_path(resolved, workspace)
    try:
        loaded = _load_suite(workspace, resolved)
    except HarnessEvalAssetError as exc:
        return HarnessEvalSuiteResult(
            suite_id="unknown",
            title="无法加载 Eval Suite",
            suite_path=display,
            status=EvalRunStatus.EVALUATION_ERROR,
            code=exc.code,
            message=str(exc),
            duration_ms=_elapsed_ms(started),
        )
    return _evaluate_loaded_suite(workspace, resolved, loaded, started=started)


def evaluate_declared_suites(
    workspace_root: str | Path,
    declared_suites: tuple[str, ...] | list[str],
    target: str | None,
    *,
    profile_digest: str | None = None,
    profile_trusted: bool = False,
) -> HarnessEvalReport:
    """Evaluate only Profile-declared suites, selected by exact path or suite id."""
    started = time.perf_counter()
    workspace = Path(workspace_root).expanduser().resolve()
    declared = tuple(str(value).strip().replace("\\", "/") for value in declared_suites)
    requested = str(target or "all").strip().replace("\\", "/") or "all"
    if not declared:
        return _report_error(
            requested,
            "no_suites_declared",
            "当前 Harness Profile 未声明 evals.suites。请先添加离线 Suite 路径。",
            started,
        )

    selected = declared
    if requested != "all":
        if requested in declared:
            selected = (requested,)
        else:
            matches: list[str] = []
            for path_text in declared:
                candidate = (workspace / path_text).resolve(strict=False)
                try:
                    suite = _load_suite(workspace, candidate).suite
                except HarnessEvalAssetError:
                    continue
                if suite.id == requested:
                    matches.append(path_text)
            if len(matches) != 1:
                return _report_error(
                    requested,
                    "suite_not_declared",
                    "未找到唯一匹配的 Eval Suite；只能运行当前 Profile "
                    "evals.suites 中声明的 id 或路径。",
                    started,
                )
            selected = (matches[0],)

    source_before, source_code = _capture_baseline_source(
        workspace,
        enabled=profile_digest is not None,
    )
    raw_results = tuple(_evaluate_suite_file_raw(workspace, path) for path in selected)
    source_after, source_code = _capture_baseline_source_after(
        workspace,
        source_before,
        source_code,
    )
    results = tuple(
        _attach_baseline_identity(
            result,
            workspace=workspace,
            profile_digest=profile_digest,
            profile_trusted=profile_trusted,
            source_before=source_before,
            source_after=source_after,
            source_code=source_code,
        )
        for result in raw_results
    )
    status = _aggregate_status(results)
    return HarnessEvalReport(
        requested=requested,
        status=status,
        suites=results,
        duration_ms=_elapsed_ms(started),
    )


def _load_suite(workspace: Path, path: Path) -> _LoadedSuite:
    if not _is_relative_to(path, workspace):
        raise HarnessEvalAssetError(
            "suite_outside_workspace",
            "Eval Suite 必须位于当前工作区内。",
        )
    raw = _read_bounded(path, MAX_SUITE_BYTES, kind="Eval Suite")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessEvalAssetError(
            "suite_invalid_encoding",
            "Eval Suite 必须使用 UTF-8 编码。",
        ) from exc
    try:
        payload: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HarnessEvalAssetError(
            "suite_invalid_yaml",
            "Eval Suite YAML 语法无效。",
        ) from exc
    try:
        suite = HarnessEvalSuite.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({str(item["loc"][0]) for item in exc.errors() if item["loc"]})
        suffix = f"（字段：{', '.join(fields[:8])}）" if fields else ""
        raise HarnessEvalAssetError(
            "suite_schema_invalid",
            f"Eval Suite schema version 1 校验失败{suffix}。",
        ) from exc
    return _LoadedSuite(suite=suite, sha256=hashlib.sha256(raw).hexdigest())


def _evaluate_loaded_suite(
    workspace: Path,
    path: Path,
    loaded: _LoadedSuite,
    *,
    started: float,
) -> HarnessEvalSuiteResult:
    suite = loaded.suite
    results: list[HarnessEvalCaseResult] = []
    deadline = started + (suite.budget.max_duration_ms / 1_000)
    budget_exhausted = False
    for case in suite.cases:
        if time.perf_counter() >= deadline:
            results.append(_skipped_case(case, "suite_budget_exhausted"))
            budget_exhausted = True
            continue
        results.append(_evaluate_protocol_case(workspace, path.parent, case))
        if time.perf_counter() > deadline:
            budget_exhausted = True

    if any(case.status is EvalCaseStatus.IMPLEMENTATION_FAILURE for case in results):
        status = EvalRunStatus.FAILED
    elif any(case.status is EvalCaseStatus.EVALUATION_ERROR for case in results):
        status = EvalRunStatus.FAILED
    elif any(case.status is EvalCaseStatus.SKIPPED for case in results):
        status = EvalRunStatus.FAILED
    elif budget_exhausted:
        status = EvalRunStatus.FAILED
    else:
        status = EvalRunStatus.PASSED
    return HarnessEvalSuiteResult(
        suite_id=suite.id,
        title=suite.title,
        suite_path=_display_path(path, workspace),
        suite_sha256=loaded.sha256,
        status=status,
        cases=tuple(results),
        code="suite_budget_exhausted" if budget_exhausted else "",
        message=(
            "Suite 总时间预算已耗尽；已完成结果保留，未开始 case 已跳过。"
            if budget_exhausted
            else ""
        ),
        comparison_policy=suite.comparison_policy,
        duration_ms=_elapsed_ms(started),
    )


def _evaluate_protocol_case(
    workspace: Path,
    suite_root: Path,
    case: HarnessEvalCase,
) -> HarnessEvalCaseResult:
    started = time.perf_counter()
    try:
        payload = _load_fixture(workspace, suite_root, case)
    except HarnessEvalAssetError as exc:
        return HarnessEvalCaseResult(
            case_id=case.id,
            runner=case.runner,
            status=EvalCaseStatus.EVALUATION_ERROR,
            expected=case.expected,
            primary_metric=case.metrics.primary,
            guardrails=_initial_guardrails(case),
            code=exc.code,
            message=str(exc),
            duration_ms=_elapsed_ms(started),
        )

    actual = _run_protocol_hello(payload)
    matches = _matches_expected(case, actual)
    duration_ms = _elapsed_ms(started)
    if duration_ms > case.budget.max_duration_ms:
        return HarnessEvalCaseResult(
            case_id=case.id,
            runner=case.runner,
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
            expected=case.expected,
            actual=actual,
            primary_metric=case.metrics.primary,
            guardrails=_initial_guardrails(case),
            code="case_budget_exceeded",
            message=(
                f"协议实现耗时 {duration_ms:.2f}ms，超过 case 预算 "
                f"{case.budget.max_duration_ms}ms。"
            ),
            duration_ms=duration_ms,
        )
    if matches:
        return HarnessEvalCaseResult(
            case_id=case.id,
            runner=case.runner,
            status=EvalCaseStatus.PASSED,
            expected=case.expected,
            actual=actual,
            primary_metric=case.metrics.primary,
            guardrails=_initial_guardrails(case),
            message="协议行为符合预期。",
            duration_ms=duration_ms,
        )
    return HarnessEvalCaseResult(
        case_id=case.id,
        runner=case.runner,
        status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
        expected=case.expected,
        actual=actual,
        primary_metric=case.metrics.primary,
        guardrails=_initial_guardrails(case),
        code="protocol_outcome_mismatch",
        message=(
            f"预期 {case.expected.outcome}/"
            f"{case.expected.error_code or case.expected.selected_version}，"
            f"实际 {actual.outcome}/{actual.error_code or actual.selected_version}。"
        ),
        duration_ms=duration_ms,
    )


def _load_fixture(
    workspace: Path,
    suite_root: Path,
    case: HarnessEvalCase,
) -> dict[str, Any]:
    path = (suite_root / case.fixture.path).resolve(strict=False)
    if not _is_relative_to(path, suite_root) or not _is_relative_to(path, workspace):
        raise HarnessEvalAssetError(
            "fixture_outside_suite",
            f"Eval fixture 越过 Suite 目录：{case.fixture.path}",
        )
    raw = _read_bounded(path, MAX_FIXTURE_BYTES, kind="Eval fixture")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != case.fixture.sha256:
        raise HarnessEvalAssetError(
            "fixture_digest_mismatch",
            f"Eval fixture digest 不匹配：{case.fixture.path}",
        )
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessEvalAssetError(
            "fixture_invalid_json",
            f"Eval fixture 不是 UTF-8 JSON：{case.fixture.path}",
        ) from exc
    if not isinstance(payload, dict):
        raise HarnessEvalAssetError(
            "fixture_invalid_root",
            f"Eval fixture 根节点必须是对象：{case.fixture.path}",
        )
    return payload


def _run_protocol_hello(payload: dict[str, Any]) -> HarnessProtocolActual:
    try:
        record = normalize_client_record(payload)
    except ValueError:
        return HarnessProtocolActual(outcome="rejected", error_code="bad_request")
    if record.get("type") != "hello":
        return HarnessProtocolActual(outcome="rejected", error_code="bad_request")
    try:
        negotiated = negotiate_hello(record["payload"])
    except ProtocolNegotiationError as exc:
        return HarnessProtocolActual(outcome="rejected", error_code=exc.code)
    return HarnessProtocolActual(
        outcome="accepted",
        selected_version=int(negotiated["selected_version"]),
        capabilities=tuple(str(value) for value in negotiated["capabilities"]),
    )


def _matches_expected(case: HarnessEvalCase, actual: HarnessProtocolActual) -> bool:
    expected = case.expected
    return (
        actual.outcome == expected.outcome
        and actual.error_code == expected.error_code
        and actual.selected_version == expected.selected_version
        and actual.capabilities == expected.capabilities
    )


def _read_bounded(path: Path, limit: int, *, kind: str) -> bytes:
    try:
        if not path.is_file():
            raise HarnessEvalAssetError(
                f"{kind.lower().replace(' ', '_')}_missing",
                f"{kind} 不存在或不是普通文件：{path.name}",
            )
        if path.stat().st_size > limit:
            raise HarnessEvalAssetError(
                f"{kind.lower().replace(' ', '_')}_too_large",
                f"{kind} 超过 {limit // 1024} KiB 上限：{path.name}",
            )
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except HarnessEvalAssetError:
        raise
    except OSError as exc:
        raise HarnessEvalAssetError(
            f"{kind.lower().replace(' ', '_')}_unreadable",
            f"{kind} 无法读取：{path.name}",
        ) from exc
    if len(raw) > limit:
        raise HarnessEvalAssetError(
            f"{kind.lower().replace(' ', '_')}_too_large",
            f"{kind} 超过 {limit // 1024} KiB 上限：{path.name}",
        )
    return raw


def _skipped_case(case: HarnessEvalCase, code: str) -> HarnessEvalCaseResult:
    return HarnessEvalCaseResult(
        case_id=case.id,
        runner=case.runner,
        status=EvalCaseStatus.SKIPPED,
        expected=case.expected,
        primary_metric=case.metrics.primary,
        guardrails=_initial_guardrails(case),
        code=code,
        message="Suite 时间预算已耗尽，本 case 未执行。",
    )


def _aggregate_status(results: tuple[HarnessEvalSuiteResult, ...]) -> EvalRunStatus:
    if all(result.status is EvalRunStatus.PASSED for result in results):
        return EvalRunStatus.PASSED
    if any(result.status is EvalRunStatus.FAILED for result in results):
        return EvalRunStatus.FAILED
    return EvalRunStatus.EVALUATION_ERROR


def _report_error(
    requested: str,
    code: str,
    message: str,
    started: float,
) -> HarnessEvalReport:
    return HarnessEvalReport(
        requested=requested,
        status=EvalRunStatus.EVALUATION_ERROR,
        code=code,
        message=message,
        duration_ms=_elapsed_ms(started),
    )


def render_harness_eval(
    result: HarnessEvalReport | HarnessEvalSuiteResult,
) -> str:
    """Render one compact Chinese report shared by slash commands and Agent tools."""
    suites = result.suites if isinstance(result, HarnessEvalReport) else (result,)
    status = result.status
    lines = ["## Harness 离线 Eval", ""]
    if not suites:
        lines.extend([
            f"- 状态：`{status}`",
            f"- 错误：{result.message}",
            "- 下一步：检查 `.naumi/harness.yaml` 的 `evals.suites` 声明。",
        ])
        return "\n".join(lines)
    totals = {
        "cases": sum(len(suite.cases) for suite in suites),
        "passed": sum(suite.passed for suite in suites),
        "implementation": sum(suite.implementation_failures for suite in suites),
        "evaluation": sum(suite.evaluation_errors for suite in suites),
        "skipped": sum(suite.skipped for suite in suites),
    }
    lines.extend([
        f"- 状态：`{status}`",
        f"- Suite：{len(suites)} · Case：{totals['cases']}",
        (
            f"- 通过 {totals['passed']} · 实现回归 {totals['implementation']} · "
            f"评测错误 {totals['evaluation']} · 跳过 {totals['skipped']}"
        ),
    ])
    for suite in suites:
        lines.extend(["", f"### {suite.title} (`{suite.suite_id}`)"])
        if suite.baseline_identity is not None:
            identity = suite.baseline_identity
            promotion = "可晋升" if identity.baseline_eligible else "不可晋升"
            lines.append(
                f"- Baseline：`{identity.identity_sha256[:12]}` · {promotion}"
            )
            if identity.warnings:
                lines.append(f"- Baseline 提示：{identity.warnings[0]}")
        elif suite.baseline_identity_code:
            lines.append(
                "- Baseline：不可用（"
                + _baseline_identity_message(suite.baseline_identity_code)
                + "）"
            )
        if suite.message:
            lines.append(f"- 评测错误 `{suite.code}`：{suite.message}")
        for case in suite.cases:
            icon = "通过" if case.status is EvalCaseStatus.PASSED else str(case.status)
            lines.append(f"- `{case.case_id}`：{icon} · {case.message}")
    if totals["implementation"]:
        lines.append(
            "\n下一步：检查生产协议行为与 expected 的差异，"
            "确认后修复实现或更新有证据的预期。"
        )
    elif totals["evaluation"] or totals["skipped"] or any(suite.message for suite in suites):
        lines.append("\n下一步：修复 Suite schema、fixture integrity 或预算后重新运行。")
    return "\n".join(lines)


def _capture_baseline_source(
    workspace: Path,
    *,
    enabled: bool,
) -> tuple[HarnessEvalSourceIdentity | None, str]:
    if not enabled:
        return None, ""
    try:
        return capture_eval_source_identity(workspace), ""
    except (TreeFingerprintError, OSError):
        return None, "baseline_source_unavailable"


def _capture_baseline_source_after(
    workspace: Path,
    source_before: HarnessEvalSourceIdentity | None,
    source_code: str,
) -> tuple[HarnessEvalSourceIdentity | None, str]:
    if source_before is None:
        return None, source_code
    try:
        return capture_eval_source_identity(workspace), source_code
    except (TreeFingerprintError, OSError):
        return None, "baseline_source_unavailable"


def _attach_baseline_identity(
    result: HarnessEvalSuiteResult,
    *,
    workspace: Path,
    profile_digest: str | None,
    profile_trusted: bool,
    source_before: HarnessEvalSourceIdentity | None,
    source_after: HarnessEvalSourceIdentity | None,
    source_code: str,
) -> HarnessEvalSuiteResult:
    if profile_digest is None:
        return result
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
    if not result.suite_sha256 or result.suite_id == "unknown":
        return result.model_copy(
            update={"baseline_identity_code": "baseline_suite_unavailable"}
        )
    result = _verify_static_side_effect_guardrails(result)
    try:
        configuration = HarnessEvalConfigurationIdentity.create(
            suite_id=result.suite_id,
            suite_sha256=result.suite_sha256,
            profile_sha256=profile_digest,
            policy_sha256=result.policy_sha256,
            runner_version=PROTOCOL_HELLO_RUNNER_VERSION,
            repetitions=1,
            live=False,
        )
        identity = build_eval_baseline_identity(
            workspace,
            configuration=configuration,
            profile_trusted=profile_trusted,
            source_identity=source_before,
        )
    except ValidationError:
        return result.model_copy(
            update={"baseline_identity_code": "baseline_configuration_invalid"}
        )
    return result.model_copy(update={"baseline_identity": identity})


def _initial_guardrails(
    case: HarnessEvalCase,
) -> tuple[HarnessEvalGuardrailResult, ...]:
    return tuple(
        HarnessEvalGuardrailResult(
            guardrail=guardrail,
            status=(
                EvalGuardrailStatus.PASSED
                if guardrail == "no_model"
                else EvalGuardrailStatus.UNVERIFIED
            ),
        )
        for guardrail in case.metrics.guardrails
    )


def _verify_static_side_effect_guardrails(
    result: HarnessEvalSuiteResult,
) -> HarnessEvalSuiteResult:
    cases = tuple(
        case.model_copy(
            update={
                "guardrails": tuple(
                    item.model_copy(update={"status": EvalGuardrailStatus.PASSED})
                    if item.guardrail == "no_side_effect"
                    else item
                    for item in case.guardrails
                )
            }
        )
        for case in result.cases
    )
    return result.model_copy(update={"cases": cases})


def _baseline_identity_message(code: str) -> str:
    return {
        "baseline_source_unavailable": "当前工作区不是可验证的 Git 仓库",
        "baseline_source_changed": "源码状态在评测期间发生变化",
        "baseline_suite_unavailable": "Suite 未能生成有效摘要",
        "baseline_configuration_invalid": "Baseline 配置身份无效",
    }.get(code, "Baseline identity 暂不可用")


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1_000)


def _display_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
