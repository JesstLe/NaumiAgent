"""Bounded protocol evaluation executed by an installed Naumi runtime binary."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.eval_models import (
    HarnessEvalSuite,
    HarnessProtocolActual,
    HarnessProtocolExpected,
)
from naumi_agent.ui.protocol import (
    ProtocolNegotiationError,
    negotiate_hello,
    normalize_client_record,
)

RELEASE_RUNTIME_EVAL_REQUEST_POLICY = "naumi-release-runtime-eval-request-v1"
RELEASE_RUNTIME_EVAL_PROCESS_REQUEST_POLICY = (
    "naumi-release-runtime-eval-process-request-v1"
)
RELEASE_RUNTIME_EVAL_RESPONSE_POLICY = "naumi-release-runtime-eval-response-v1"
RELEASE_RUNTIME_EVAL_RECEIPT_POLICY = "naumi-release-runtime-eval-receipt-v1"
RELEASE_RUNTIME_EVAL_RUNNER = "protocol_hello@1"
MAX_RUNTIME_EVAL_INPUT_BYTES = 256 * 1024
MAX_RUNTIME_EVAL_OUTPUT_BYTES = 512 * 1024
MAX_RUNTIME_EVAL_CASES = 100
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_ID_RE = r"^[a-z][a-z0-9_-]{0,63}$"
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleaseRuntimeEvalCaseRequest(_StrictModel):
    case_id: str = Field(pattern=_SAFE_ID_RE)
    fixture_sha256: str = Field(pattern=_SHA256_RE)
    payload_sha256: str = Field(pattern=_SHA256_RE)
    payload: dict[str, Any]
    expected: HarnessProtocolExpected
    max_duration_ms: int = Field(ge=1, le=5_000)

    @model_validator(mode="after")
    def _bounded_payload(self) -> Self:
        _validate_payload_shape(self.payload)
        encoded = _canonical_json(self.payload)
        if len(encoded) > 64 * 1024:
            raise ValueError("Runtime Eval fixture 超过 64 KiB。")
        if not hmac.compare_digest(
            hashlib.sha256(encoded).hexdigest(),
            self.payload_sha256,
        ):
            raise ValueError("Runtime Eval payload 摘要不一致。")
        return self


class ReleaseRuntimeEvalRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-eval-request-v1"] = (
        RELEASE_RUNTIME_EVAL_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^relruntimeevalreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=_SAFE_ID_RE)
    suite_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: Literal["protocol_hello@1"] = RELEASE_RUNTIME_EVAL_RUNNER
    cases: tuple[ReleaseRuntimeEvalCaseRequest, ...] = Field(
        min_length=1,
        max_length=MAX_RUNTIME_EVAL_CASES,
    )

    @model_validator(mode="after")
    def _exact(self) -> Self:
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("Runtime Eval case id 不得重复。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, digest):
            raise ValueError("Runtime Eval Request 摘要不一致。")
        if self.request_id != f"relruntimeevalreq_{digest[:24]}":
            raise ValueError("Runtime Eval Request identity 不一致。")
        if len(self.model_dump_json().encode("utf-8")) > MAX_RUNTIME_EVAL_INPUT_BYTES:
            raise ValueError("Runtime Eval Request 超过 256 KiB。")
        return self


class ReleaseRuntimeEvalProcessCaseRequest(_StrictModel):
    case_id: str = Field(pattern=_SAFE_ID_RE)
    fixture_sha256: str = Field(pattern=_SHA256_RE)
    payload_sha256: str = Field(pattern=_SHA256_RE)
    payload: dict[str, Any]
    max_duration_ms: int = Field(ge=1, le=5_000)

    @model_validator(mode="after")
    def _bounded_payload(self) -> Self:
        _validate_payload_shape(self.payload)
        encoded = _canonical_json(self.payload)
        if len(encoded) > 64 * 1024:
            raise ValueError("Runtime Eval process payload 超过 64 KiB。")
        if not hmac.compare_digest(
            hashlib.sha256(encoded).hexdigest(),
            self.payload_sha256,
        ):
            raise ValueError("Runtime Eval process payload 摘要不一致。")
        return self


class ReleaseRuntimeEvalProcessRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-eval-process-request-v1"] = (
        RELEASE_RUNTIME_EVAL_PROCESS_REQUEST_POLICY
    )
    process_request_id: str = Field(pattern=r"^relruntimeevalproc_[0-9a-f]{24}$")
    process_request_sha256: str = Field(pattern=_SHA256_RE)
    authority_request_id: str = Field(pattern=r"^relruntimeevalreq_[0-9a-f]{24}$")
    authority_request_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=_SAFE_ID_RE)
    suite_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: Literal["protocol_hello@1"] = RELEASE_RUNTIME_EVAL_RUNNER
    cases: tuple[ReleaseRuntimeEvalProcessCaseRequest, ...] = Field(
        min_length=1,
        max_length=MAX_RUNTIME_EVAL_CASES,
    )

    @model_validator(mode="after")
    def _exact(self) -> Self:
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("Runtime Eval process case id 不得重复。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"process_request_id", "process_request_sha256"},
            )
        )
        if not hmac.compare_digest(self.process_request_sha256, digest):
            raise ValueError("Runtime Eval Process Request 摘要不一致。")
        if self.process_request_id != f"relruntimeevalproc_{digest[:24]}":
            raise ValueError("Runtime Eval Process Request identity 不一致。")
        if len(self.model_dump_json().encode("utf-8")) > MAX_RUNTIME_EVAL_INPUT_BYTES:
            raise ValueError("Runtime Eval Process Request 超过 256 KiB。")
        return self


class ReleaseRuntimeEvalCaseResult(_StrictModel):
    case_id: str = Field(pattern=_SAFE_ID_RE)
    fixture_sha256: str = Field(pattern=_SHA256_RE)
    actual: HarnessProtocolActual
    no_model: Literal[True] = True
    no_side_effect: Literal[True] = True
    duration_ms: float = Field(ge=0, le=5_000)


class ReleaseRuntimeEvalResponse(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-eval-response-v1"] = (
        RELEASE_RUNTIME_EVAL_RESPONSE_POLICY
    )
    response_id: str = Field(pattern=r"^relruntimeevalresp_[0-9a-f]{24}$")
    response_sha256: str = Field(pattern=_SHA256_RE)
    process_request_id: str = Field(pattern=r"^relruntimeevalproc_[0-9a-f]{24}$")
    process_request_sha256: str = Field(pattern=_SHA256_RE)
    authority_request_id: str = Field(pattern=r"^relruntimeevalreq_[0-9a-f]{24}$")
    authority_request_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: Literal["protocol_hello@1"] = RELEASE_RUNTIME_EVAL_RUNNER
    results: tuple[ReleaseRuntimeEvalCaseResult, ...] = Field(
        min_length=1,
        max_length=MAX_RUNTIME_EVAL_CASES,
    )
    evaluated_at: str = Field(min_length=1, max_length=100)

    @field_validator("evaluated_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        return _aware(value).isoformat()

    @model_validator(mode="after")
    def _exact(self) -> Self:
        ids = tuple(item.case_id for item in self.results)
        if len(ids) != len(set(ids)):
            raise ValueError("Runtime Eval Response case id 不得重复。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"response_id", "response_sha256"})
        )
        if not hmac.compare_digest(self.response_sha256, digest):
            raise ValueError("Runtime Eval Response 摘要不一致。")
        if self.response_id != f"relruntimeevalresp_{digest[:24]}":
            raise ValueError("Runtime Eval Response identity 不一致。")
        if len(self.model_dump_json().encode("utf-8")) > MAX_RUNTIME_EVAL_OUTPUT_BYTES:
            raise ValueError("Runtime Eval Response 超过 512 KiB。")
        return self


class ReleaseRuntimeEvalReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-eval-receipt-v1"] = (
        RELEASE_RUNTIME_EVAL_RECEIPT_POLICY
    )
    receipt_id: str = Field(pattern=r"^relruntimeeval_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    request: ReleaseRuntimeEvalRequest
    process_request: ReleaseRuntimeEvalProcessRequest
    response: ReleaseRuntimeEvalResponse
    arguments: tuple[Literal["--runtime-eval-json"], ...] = ("--runtime-eval-json",)
    input_bytes: int = Field(ge=1, le=MAX_RUNTIME_EVAL_INPUT_BYTES)
    output_bytes: int = Field(ge=1, le=MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    exit_code: Literal[0] = 0
    runtime_process_executed: Literal[True] = True
    passed_cases: int = Field(ge=0, le=MAX_RUNTIME_EVAL_CASES)
    failed_cases: int = Field(ge=0, le=MAX_RUNTIME_EVAL_CASES)
    all_cases_passed: bool
    evaluated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        derived_process = build_runtime_eval_process_request(self.request)
        try:
            matches = _result_matches(self.request, self.response)
            result_binding_valid = all(
                result.case_id == case.case_id
                and result.fixture_sha256 == case.fixture_sha256
                for case, result in zip(
                    self.request.cases,
                    self.response.results,
                    strict=True,
                )
            )
        except ValueError as exc:
            raise ValueError("Runtime Eval Receipt case 数量不一致。") from exc
        passed = sum(matches)
        if not (
            self.process_request == derived_process
            and self.response.process_request_id == self.process_request.process_request_id
            and self.response.process_request_sha256
            == self.process_request.process_request_sha256
            and self.response.authority_request_id == self.request.request_id
            and self.response.authority_request_sha256 == self.request.request_sha256
            and tuple(item.case_id for item in self.response.results)
            == tuple(item.case_id for item in self.request.cases)
            and len(self.response.results) == len(self.request.cases)
            and result_binding_valid
            and self.passed_cases == passed
            and self.failed_cases == len(matches) - passed
            and self.all_cases_passed is (passed == len(matches))
            and self.evaluated_at == self.response.evaluated_at
        ):
            raise ValueError("Runtime Eval Receipt request/response 绑定不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Runtime Eval Receipt 摘要不一致。")
        if self.receipt_id != f"relruntimeeval_{digest[:24]}":
            raise ValueError("Runtime Eval Receipt identity 不一致。")
        return self


class ReleaseRuntimeEvalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_protocol_hello_request(
    workspace_root: str | Path,
    suite_path: str | Path,
) -> ReleaseRuntimeEvalRequest:
    workspace = Path(workspace_root).expanduser().resolve(strict=True)
    candidate = Path(suite_path).expanduser()
    resolved = (
        (workspace / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if not _is_relative_to(resolved, workspace):
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_suite_outside_workspace",
            "Runtime Eval Suite 必须位于 workspace 内。",
        )
    try:
        raw = _read_bounded(resolved, 256 * 1024)
        suite = HarnessEvalSuite.model_validate(yaml.safe_load(raw))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_suite_invalid",
            "Runtime Eval Suite 无效或无法读取。",
        ) from exc
    if any(case.runner != "protocol_hello" for case in suite.cases):
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_runner_unsupported",
            "Installed Runtime Eval 当前只接受 protocol_hello runner。",
        )
    suite_root = resolved.parent
    cases: list[dict[str, Any]] = []
    try:
        for case in suite.cases:
            fixture = (suite_root / case.fixture.path).resolve()
            if not _is_relative_to(fixture, suite_root) or not _is_relative_to(fixture, workspace):
                raise ValueError("fixture 越过 Suite 边界")
            encoded = _read_bounded(fixture, 64 * 1024)
            if not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), case.fixture.sha256):
                raise ValueError("fixture digest 不一致")
            payload = json.loads(encoded.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("fixture 根节点无效")
            cases.append({
                "case_id": case.id,
                "fixture_sha256": case.fixture.sha256,
                "payload_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
                "payload": payload,
                "expected": case.expected.model_dump(mode="json"),
                "max_duration_ms": case.budget.max_duration_ms,
            })
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_fixture_invalid",
            "Runtime Eval fixture 无效、越界或摘要不一致。",
        ) from exc
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_EVAL_REQUEST_POLICY,
        "suite_id": suite.id,
        "suite_sha256": hashlib.sha256(raw).hexdigest(),
        "runner_version": RELEASE_RUNTIME_EVAL_RUNNER,
        "cases": cases,
    }
    digest = _digest(core)
    try:
        return ReleaseRuntimeEvalRequest.model_validate({
            **core,
            "request_id": f"relruntimeevalreq_{digest[:24]}",
            "request_sha256": digest,
        })
    except ValueError as exc:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_request_invalid",
            "Runtime Eval Request 无法验证。",
        ) from exc


def execute_runtime_eval_request(
    request: ReleaseRuntimeEvalRequest,
    *,
    evaluated_at: str | None = None,
) -> ReleaseRuntimeEvalResponse:
    item = ReleaseRuntimeEvalRequest.model_validate_json(request.model_dump_json())
    process_request = build_runtime_eval_process_request(item)
    return execute_runtime_eval_process_request(
        process_request,
        evaluated_at=evaluated_at,
    )


def build_runtime_eval_process_request(
    request: ReleaseRuntimeEvalRequest,
) -> ReleaseRuntimeEvalProcessRequest:
    item = ReleaseRuntimeEvalRequest.model_validate_json(request.model_dump_json())
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_EVAL_PROCESS_REQUEST_POLICY,
        "authority_request_id": item.request_id,
        "authority_request_sha256": item.request_sha256,
        "suite_id": item.suite_id,
        "suite_sha256": item.suite_sha256,
        "runner_version": item.runner_version,
        "cases": [
            {
                "case_id": case.case_id,
                "fixture_sha256": case.fixture_sha256,
                "payload_sha256": case.payload_sha256,
                "payload": case.payload,
                "max_duration_ms": case.max_duration_ms,
            }
            for case in item.cases
        ],
    }
    digest = _digest(core)
    return ReleaseRuntimeEvalProcessRequest.model_validate({
        **core,
        "process_request_id": f"relruntimeevalproc_{digest[:24]}",
        "process_request_sha256": digest,
    })


def execute_runtime_eval_process_request(
    request: ReleaseRuntimeEvalProcessRequest,
    *,
    evaluated_at: str | None = None,
) -> ReleaseRuntimeEvalResponse:
    item = ReleaseRuntimeEvalProcessRequest.model_validate_json(request.model_dump_json())
    results: list[dict[str, Any]] = []
    for case in item.cases:
        started = time.perf_counter()
        actual = _run_protocol_hello(case.payload)
        duration_ms = min((time.perf_counter() - started) * 1000, 5_000.0)
        results.append({
            "case_id": case.case_id,
            "fixture_sha256": case.fixture_sha256,
            "actual": actual.model_dump(mode="json"),
            "no_model": True,
            "no_side_effect": True,
            "duration_ms": duration_ms,
        })
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_EVAL_RESPONSE_POLICY,
        "process_request_id": item.process_request_id,
        "process_request_sha256": item.process_request_sha256,
        "authority_request_id": item.authority_request_id,
        "authority_request_sha256": item.authority_request_sha256,
        "runner_version": RELEASE_RUNTIME_EVAL_RUNNER,
        "results": results,
        "evaluated_at": _aware(evaluated_at or datetime.now(UTC).isoformat()).isoformat(),
    }
    digest = _digest(core)
    return ReleaseRuntimeEvalResponse.model_validate({
        **core,
        "response_id": f"relruntimeevalresp_{digest[:24]}",
        "response_sha256": digest,
    })


def parse_and_execute_runtime_eval(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_RUNTIME_EVAL_INPUT_BYTES:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_input_oversized",
            "Runtime Eval 输入为空或超过 256 KiB。",
        )
    try:
        request = ReleaseRuntimeEvalProcessRequest.model_validate_json(raw)
        response = execute_runtime_eval_process_request(request)
    except ValueError as exc:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_input_invalid",
            "Runtime Eval 输入无效。",
        ) from exc
    encoded = response.model_dump_json().encode("utf-8")
    if len(encoded) > MAX_RUNTIME_EVAL_OUTPUT_BYTES:
        raise ReleaseRuntimeEvalError(
            "release_runtime_eval_output_oversized",
            "Runtime Eval 输出超过 512 KiB。",
        )
    return encoded


def build_runtime_eval_receipt(
    *,
    slot_id: str,
    slot_sha256: str,
    manifest_sha256: str,
    binary_sha256: str,
    request: ReleaseRuntimeEvalRequest,
    process_request: ReleaseRuntimeEvalProcessRequest,
    response: ReleaseRuntimeEvalResponse,
    input_bytes: int,
    output_bytes: int,
) -> ReleaseRuntimeEvalReceipt:
    matches = _result_matches(request, response)
    passed = sum(matches)
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_EVAL_RECEIPT_POLICY,
        "slot_id": slot_id,
        "slot_sha256": slot_sha256,
        "manifest_sha256": manifest_sha256,
        "binary_sha256": binary_sha256,
        "request": request.model_dump(mode="json"),
        "process_request": process_request.model_dump(mode="json"),
        "response": response.model_dump(mode="json"),
        "arguments": ["--runtime-eval-json"],
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
        "exit_code": 0,
        "runtime_process_executed": True,
        "passed_cases": passed,
        "failed_cases": len(matches) - passed,
        "all_cases_passed": passed == len(matches),
        "evaluated_at": response.evaluated_at,
    }
    digest = _digest(core)
    return ReleaseRuntimeEvalReceipt.model_validate({
        **core,
        "receipt_id": f"relruntimeeval_{digest[:24]}",
        "receipt_sha256": digest,
    })


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


def _matches(expected: HarnessProtocolExpected, actual: HarnessProtocolActual) -> bool:
    return (
        actual.outcome == expected.outcome
        and actual.error_code == expected.error_code
        and actual.selected_version == expected.selected_version
        and actual.capabilities == expected.capabilities
    )


def _result_matches(
    request: ReleaseRuntimeEvalRequest,
    response: ReleaseRuntimeEvalResponse,
) -> tuple[bool, ...]:
    return tuple(
        _matches(case.expected, result.actual)
        and result.duration_ms <= case.max_duration_ms
        for case, result in zip(request.cases, response.results, strict=True)
    )


def _read_bounded(path: Path, limit: int) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise OSError("文件不存在、不是普通文件或超过上限")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise OSError("文件超过上限")
    return raw


def _validate_payload_shape(payload: dict[str, Any]) -> None:
    stack: list[tuple[Any, int]] = [(payload, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 2_048 or depth > 16:
            raise ValueError("Runtime Eval payload 结构超过安全上限。")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ValueError("Runtime Eval payload key 无效。")
                if _SENSITIVE_KEY_RE.search(key):
                    raise ValueError("Runtime Eval payload 不得包含敏感字段。")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > 4_096:
                raise ValueError("Runtime Eval payload 字符串超过安全上限。")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("Runtime Eval payload 含非 JSON 值。")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Eval 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


__all__ = [
    "MAX_RUNTIME_EVAL_INPUT_BYTES",
    "MAX_RUNTIME_EVAL_OUTPUT_BYTES",
    "RELEASE_RUNTIME_EVAL_RECEIPT_POLICY",
    "RELEASE_RUNTIME_EVAL_PROCESS_REQUEST_POLICY",
    "RELEASE_RUNTIME_EVAL_REQUEST_POLICY",
    "RELEASE_RUNTIME_EVAL_RESPONSE_POLICY",
    "RELEASE_RUNTIME_EVAL_RUNNER",
    "ReleaseRuntimeEvalCaseRequest",
    "ReleaseRuntimeEvalCaseResult",
    "ReleaseRuntimeEvalError",
    "ReleaseRuntimeEvalProcessCaseRequest",
    "ReleaseRuntimeEvalProcessRequest",
    "ReleaseRuntimeEvalReceipt",
    "ReleaseRuntimeEvalRequest",
    "ReleaseRuntimeEvalResponse",
    "build_protocol_hello_request",
    "build_runtime_eval_process_request",
    "build_runtime_eval_receipt",
    "execute_runtime_eval_request",
    "execute_runtime_eval_process_request",
    "parse_and_execute_runtime_eval",
]
