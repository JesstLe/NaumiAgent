"""Agent tools for Harness status, knowledge, and trusted checks."""

from __future__ import annotations

import re
from typing import Any

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptError
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantError
from naumi_agent.harness.eval import render_harness_eval
from naumi_agent.harness.eval_surface import (
    render_eval_baseline_status,
    render_eval_batch_status,
    render_eval_comparison_run_status,
    render_eval_promotion_status,
)
from naumi_agent.harness.explain import render_harness_explanation
from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchCheckpoint,
    HarnessSandboxBatchError,
)
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalExecutionError
from naumi_agent.harness.sandbox_request import HarnessSandboxEvalRequestError
from naumi_agent.harness.sandbox_service import (
    HarnessSandboxEvalServiceError,
    render_sandbox_eval_batch_receipt,
)
from naumi_agent.harness.service import (
    HarnessService,
    render_harness_check,
    render_harness_doctor,
    render_harness_knowledge,
    render_harness_replay,
    render_harness_status,
)
from naumi_agent.harness.store import HarnessStoreError
from naumi_agent.runtime.ports.events import LegacyEventCallback, RuntimeEventType
from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.harness_protocol import harness_sandbox_eval_progress_payload


def create_harness_tools(service: HarnessService) -> list[Tool]:
    return [
        HarnessStatusTool(service),
        HarnessDoctorTool(service),
        HarnessExplainTool(service),
        HarnessReplayTool(service),
        HarnessEvalTool(service),
        HarnessEvalReplayTool(service),
        HarnessEvalBaselineTool(service),
        HarnessEvalBatchTool(service),
        HarnessEvalSandboxTool(service),
        HarnessEvalSandboxRetryTool(service),
        HarnessEvalBaselinePromoteTool(service),
        HarnessEvalCompareTool(service),
        HarnessReadKnowledgeTool(service),
        HarnessRunCheckTool(service),
    ]


class _HarnessReadOnlyTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint="harness profile repository contract doctor trust status",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}


class HarnessStatusTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_status"

    @property
    def description(self) -> str:
        return "查看当前工作区 Harness Profile 的解析与信任状态"

    async def execute(self, **kwargs: Any) -> str:
        return render_harness_status(await self._service.status())


class HarnessDoctorTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_doctor"

    @property
    def description(self) -> str:
        return "只读诊断 Harness Profile、知识入口和检查定义，不执行命令"

    async def execute(self, **kwargs: Any) -> str:
        return render_harness_doctor(await self._service.doctor())


class HarnessExplainTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_explain"

    @property
    def description(self) -> str:
        return "解释当前工作区最近一次或指定 Harness 运行的完成与失败原因"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness explain run failure receipt check evidence why status"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "可选 Harness run id；省略或 latest 表示当前工作区最新运行",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = kwargs.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return "Harness 解释参数无效：run_id 必须是字符串。"
        try:
            result = await self._service.explain_run(run_id)
        except ValueError as exc:
            return f"Harness 解释参数无效：{exc}"
        return render_harness_explanation(result)


class HarnessReplayTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_replay"

    @property
    def description(self) -> str:
        return "安全回放当前工作区最近一次或指定 Harness 运行，不执行工具、模型或检查"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness replay deterministic receipt evidence artifact verify history"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "可选 Harness run id；省略或 latest 表示当前工作区最新运行",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = kwargs.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return "Harness Replay 参数无效：run_id 必须是字符串。"
        try:
            result = await self._service.replay_run(run_id)
        except ValueError as exc:
            return f"Harness Replay 参数无效：{exc}"
        return render_harness_replay(result)


class HarnessEvalTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_eval"

    @property
    def description(self) -> str:
        return "运行当前 Profile 声明的离线 Harness Eval，不调用模型、命令或网络"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness eval offline protocol regression fixture suite deterministic"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1_024,
                    "description": "可选 Profile 已声明的 Suite id 或相对路径；省略表示全部",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        if suite is not None and not isinstance(suite, str):
            return "Harness Eval 参数无效：suite 必须是字符串。"
        if isinstance(suite, str) and (not suite.strip() or len(suite.strip()) > 1_024):
            return "Harness Eval 参数无效：suite 必须是 1..1024 个字符。"
        try:
            result = await self._service.eval_suites(suite)
        except ValueError as exc:
            return f"Harness Eval 参数无效：{exc}"
        return render_harness_eval(result)


class HarnessEvalReplayTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_eval_replay"

    @property
    def description(self) -> str:
        return "将既有 Harness Safe Replay 转为可比较 Eval 结果，不执行或写入任何任务"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness eval safe replay deterministic baseline regression no side effect"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "可选 Harness run id；省略或 latest 表示最新运行",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = kwargs.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return "Harness Replay Eval 参数无效：run_id 必须是字符串。"
        try:
            result = await self._service.eval_replay_run(run_id)
        except ValueError as exc:
            return f"Harness Replay Eval 参数无效：{exc}"
        return render_harness_eval(result)


class HarnessEvalBaselineTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_eval_baseline"

    @property
    def description(self) -> str:
        return "查看指定 Eval Suite 的 active Baseline 与最近权威比较回执"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness eval baseline comparison receipt regression status history"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Eval Suite id",
                }
            },
            "required": ["suite"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        if not isinstance(suite, str):
            return "Harness Baseline 参数无效：suite 必须是字符串。"
        try:
            result = await self._service.eval_baseline_status(suite)
        except ValueError as exc:
            return f"Harness Baseline 参数无效：{exc}"
        return render_eval_baseline_status(result)


class HarnessEvalBatchTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_batch"

    @property
    def description(self) -> str:
        return "重复运行一个离线 Eval Suite，并把全部样本保存为不可变 Candidate batch"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint="harness repeated eval candidate batch persist samples",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "minLength": 1, "maxLength": 1_024},
                "repetitions": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 100,
                    "default": 5,
                },
                "batch_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["suite"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        repetitions = kwargs.get("repetitions", 5)
        batch_id = kwargs.get("batch_id")
        if not isinstance(suite, str):
            return "Harness Eval Batch 参数无效：suite 必须是字符串。"
        if batch_id is not None and not isinstance(batch_id, str):
            return "Harness Eval Batch 参数无效：batch_id 必须是字符串。"
        try:
            result = await self._service.eval_repetition_batch(
                suite,
                repetitions=repetitions,
                batch_id=batch_id,
            )
        except ValueError as exc:
            return f"Harness Eval Batch 参数无效：{exc}"
        return render_eval_batch_status(result)


class HarnessEvalSandboxTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_sandbox"

    @property
    def description(self) -> str:
        return "在精确 Git revision 的隔离 Worker 中重复执行受信 Profile checks"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=(
                "harness sandbox eval profile checks repeated batch h5a validation"
            ),
            delegated_tool_names=("bash_run",),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "check_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    },
                    "minItems": 1,
                    "maxItems": 80,
                    "uniqueItems": True,
                    "description": "按执行顺序排列的 Profile check IDs",
                },
                "samples": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 100,
                    "default": 5,
                },
                "batch_id": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "当前 Runtime/会话的稳定运行标识",
                },
            },
            "required": ["check_ids", "samples", "batch_id", "run_id"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        event_callback: LegacyEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        check_ids = kwargs.get("check_ids")
        samples = kwargs.get("samples")
        batch_id = kwargs.get("batch_id")
        run_id = kwargs.get("run_id")
        if (
            not isinstance(check_ids, list)
            or not check_ids
            or any(not isinstance(item, str) for item in check_ids)
            or isinstance(samples, bool)
            or not isinstance(samples, int)
            or not isinstance(batch_id, str)
            or not isinstance(run_id, str)
            or not run_id.strip()
            or len(run_id.strip()) > 128
        ):
            return (
                "Harness Sandbox Eval 参数无效："
                "check_ids 必须是非空字符串数组，samples 必须是整数，"
                "batch_id 和 run_id 必须是字符串。"
            )
        normalized_check_ids = tuple(check_ids)

        async def publish_progress(
            checkpoint: HarnessSandboxBatchCheckpoint,
        ) -> None:
            if event_callback is None:
                return
            await event_callback(
                RuntimeEventType.HARNESS_SANDBOX_EVAL_PROGRESS.value,
                harness_sandbox_eval_progress_payload(
                    checkpoint,
                    batch_id=batch_id,
                    check_ids=normalized_check_ids,
                ),
            )

        try:
            receipt = await self._service.eval_sandbox(
                check_ids=normalized_check_ids,
                samples=samples,
                batch_id=batch_id,
                on_progress=publish_progress,
            )
        except (
            HarnessSandboxEvalRequestError,
            HarnessSandboxEvalServiceError,
            HarnessSandboxBatchError,
            HarnessSandboxEvalExecutionError,
            HarnessStoreError,
            PermissionDecisionReceiptError,
            RunDelegationGrantError,
        ) as exc:
            code = getattr(exc, "code", "sandbox_eval_infrastructure_error")
            return f"Harness Sandbox Eval 未完成（`{code}`）：{exc}"
        return render_sandbox_eval_batch_receipt(receipt)


class HarnessEvalSandboxRetryTool(Tool):
    """Resume one cancelled Sandbox Eval from its durable server-side request."""

    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retry"

    @property
    def description(self) -> str:
        return "使用已接受的取消回执和新执行权威恢复原 Sandbox Eval"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=(
                "harness sandbox eval retry resume cancelled batch h5a recovery"
            ),
            delegated_tool_names=("bash_run",),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "retry_action_id": {
                    "type": "string",
                    "pattern": "^hsar_[0-9a-f]{24}$",
                },
                "cancel_receipt_id": {
                    "type": "string",
                    "pattern": "^hsacr_[0-9a-f]{24}$",
                },
                "cancel_receipt_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "当前 Runtime/会话的稳定运行标识",
                },
            },
            "required": [
                "retry_action_id",
                "cancel_receipt_id",
                "cancel_receipt_sha256",
                "reason",
                "run_id",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        event_callback: LegacyEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        retry_action_id = kwargs.get("retry_action_id")
        cancel_receipt_id = kwargs.get("cancel_receipt_id")
        cancel_receipt_sha256 = kwargs.get("cancel_receipt_sha256")
        reason = kwargs.get("reason")
        run_id = kwargs.get("run_id")
        values = (
            retry_action_id,
            cancel_receipt_id,
            cancel_receipt_sha256,
            reason,
            run_id,
        )
        if (
            any(not isinstance(item, str) for item in values)
            or any(not item.strip() for item in values)
            or re.fullmatch(
                r"hsar_[0-9a-f]{24}",
                retry_action_id,
            )
            is None
            or re.fullmatch(
                r"hsacr_[0-9a-f]{24}",
                cancel_receipt_id,
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                cancel_receipt_sha256,
            )
            is None
            or reason != reason.strip()
            or len(reason) > 500
            or run_id != run_id.strip()
            or len(run_id) > 128
        ):
            return (
                "Harness Sandbox Eval retry 参数无效："
                "action、cancel receipt、reason 和 run_id 均必须提供。"
            )
        normalized_action = retry_action_id.strip().lower()
        normalized_cancel = cancel_receipt_id.strip().lower()
        normalized_cancel_sha256 = cancel_receipt_sha256.strip().lower()
        normalized_reason = reason.strip()
        manifest_metadata: tuple[str, tuple[str, ...]] | None = None

        async def publish_progress(
            checkpoint: HarnessSandboxBatchCheckpoint,
        ) -> None:
            nonlocal manifest_metadata
            if event_callback is None:
                return
            if manifest_metadata is None and self._service.store is not None:
                retry = await self._service.store.get_sandbox_admission_retry(
                    workspace_root=self._service.workspace_root,
                    action_id=normalized_action,
                )
                if retry is not None and retry.decision == "accepted":
                    stored = await self._service.store.get_sandbox_eval_request(
                        self._service.workspace_root,
                        retry.eval_request_sha256,
                    )
                    if stored is not None:
                        manifest_metadata = (
                            stored.request.batch_id,
                            tuple(item.check_id for item in stored.request.checks),
                        )
            if manifest_metadata is None:
                raise HarnessSandboxEvalServiceError(
                    "sandbox_eval_service_retry_progress_manifest_missing",
                    "Sandbox Eval retry 无法恢复进度展示所需的 Request Manifest。",
                )
            await event_callback(
                RuntimeEventType.HARNESS_SANDBOX_EVAL_PROGRESS.value,
                harness_sandbox_eval_progress_payload(
                    checkpoint,
                    batch_id=manifest_metadata[0],
                    check_ids=manifest_metadata[1],
                ),
            )

        try:
            receipt = await self._service.retry_sandbox(
                retry_action_id=normalized_action,
                cancel_receipt_id=normalized_cancel,
                cancel_receipt_sha256=normalized_cancel_sha256,
                reason=normalized_reason,
                on_progress=publish_progress,
            )
        except (
            HarnessSandboxEvalRequestError,
            HarnessSandboxEvalServiceError,
            HarnessSandboxBatchError,
            HarnessSandboxEvalExecutionError,
            HarnessStoreError,
            PermissionDecisionReceiptError,
            RunDelegationGrantError,
        ) as exc:
            code = getattr(exc, "code", "sandbox_eval_retry_infrastructure_error")
            return f"Harness Sandbox Eval retry 未完成（`{code}`）：{exc}"
        return render_sandbox_eval_batch_receipt(receipt)


class HarnessEvalBaselinePromoteTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_baseline_promote"

    @property
    def description(self) -> str:
        return "显式晋升一个完整且 eligible 的 Eval batch，并原子更新 active Baseline"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint="harness eval baseline promote candidate governance selector",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "minLength": 1, "maxLength": 64},
                "batch_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "reason": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 2_000,
                },
            },
            "required": ["suite", "batch_id", "reason"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        batch_id = kwargs.get("batch_id")
        reason = kwargs.get("reason")
        if not all(isinstance(value, str) for value in (suite, batch_id, reason)):
            return "Harness Baseline 晋升参数无效：suite、batch_id、reason 必须是字符串。"
        try:
            result = await self._service.promote_eval_baseline(
                suite,
                batch_id,
                actor="agent",
                reason=reason,
            )
        except ValueError as exc:
            return f"Harness Baseline 晋升参数无效：{exc}"
        return render_eval_promotion_status(result)


class HarnessEvalCompareTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_compare"

    @property
    def description(self) -> str:
        return "将一个完整 Candidate batch 与 active Baseline 比较并保存权威回执"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint="harness eval compare candidate active baseline receipt",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "minLength": 1, "maxLength": 64},
                "candidate_batch_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["suite", "candidate_batch_id"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        candidate = kwargs.get("candidate_batch_id")
        if not isinstance(suite, str) or not isinstance(candidate, str):
            return "Harness Eval 比较参数无效：suite 和 candidate_batch_id 必须是字符串。"
        try:
            result = await self._service.compare_eval_candidate(suite, candidate)
        except ValueError as exc:
            return f"Harness Eval 比较参数无效：{exc}"
        return render_eval_comparison_run_status(result)


class HarnessReadKnowledgeTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_read_knowledge"

    @property
    def description(self) -> str:
        return "从当前受信任仓库知识索引按查询或相对路径读取有界证据"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("path",),
            user_facing_name=self.description,
            search_hint=(
                "harness repository knowledge read source docs instructions "
                "symbol path evidence"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "知识 ID、文件名、符号或文本查询",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "工作区内的精确相对路径",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4_000,
                    "default": 2_000,
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        path = kwargs.get("path")
        max_tokens = kwargs.get("max_tokens", 2_000)
        if query is not None and not isinstance(query, str):
            return "Harness 知识读取参数无效：query 必须是字符串。"
        if path is not None and not isinstance(path, str):
            return "Harness 知识读取参数无效：path 必须是字符串。"
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            return "Harness 知识读取参数无效：max_tokens 必须是整数。"
        try:
            result = await self._service.read_knowledge(
                query=query,
                path=path,
                max_tokens=max_tokens,
            )
        except ValueError as exc:
            return f"Harness 知识读取参数无效：{exc}"
        return render_harness_knowledge(result)


class HarnessRunCheckTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_run_check"

    @property
    def description(self) -> str:
        return "运行当前受信任 Harness Profile 中精确声明的一项验证检查"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint="harness validation check test lint verify completion evidence",
            delegated_tool_names=("bash_run",),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "check_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Profile 中声明的 check id",
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "当前 Harness run 的稳定标识",
                },
            },
            "required": ["check_id", "run_id"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        check_id = kwargs.get("check_id")
        run_id = kwargs.get("run_id")
        if not isinstance(check_id, str) or not isinstance(run_id, str):
            return "Harness 检查参数无效：check_id 和 run_id 必须是字符串。"
        try:
            result = await self._service.run_check(
                check_id=check_id,
                run_id=run_id,
            )
        except ValueError as exc:
            return f"Harness 检查参数无效：{exc}"
        return render_harness_check(result)
