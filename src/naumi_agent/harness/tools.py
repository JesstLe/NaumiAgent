"""Agent tools for Harness status, knowledge, and trusted checks."""

from __future__ import annotations

import re
from typing import Any

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptError
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantError
from naumi_agent.harness.eval import render_harness_eval
from naumi_agent.harness.eval_live import (
    HarnessLiveEvalError,
    render_harness_live_eval,
)
from naumi_agent.harness.eval_live_suite import render_live_batch_status
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
from naumi_agent.harness.sandbox_retry_detail import render_sandbox_retry_detail
from naumi_agent.harness.sandbox_retry_prune import (
    render_sandbox_retry_prune_execution_receipt,
    render_sandbox_retry_prune_receipt,
)
from naumi_agent.harness.sandbox_retry_retention import (
    render_sandbox_retry_retention_preview,
)
from naumi_agent.harness.sandbox_service import (
    HarnessSandboxEvalServiceError,
    SandboxEvalProgressCallback,
    render_sandbox_eval_batch_receipt,
)
from naumi_agent.harness.service import (
    HarnessService,
    render_harness_check,
    render_harness_doctor,
    render_harness_knowledge,
    render_harness_replay,
    render_harness_status,
    render_sandbox_retry_catalog,
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
        HarnessEvalLiveTool(service),
        HarnessEvalLiveBatchTool(service),
        HarnessEvalReplayTool(service),
        HarnessEvalBaselineTool(service),
        HarnessEvalBatchTool(service),
        HarnessEvalSandboxTool(service),
        HarnessEvalSandboxRetryTool(service),
        HarnessEvalSandboxResumeTool(service),
        HarnessEvalSandboxRetryCatalogTool(service),
        HarnessEvalSandboxRetryDetailTool(service),
        HarnessEvalSandboxRetryRetentionPreviewTool(service),
        HarnessEvalSandboxRetryPruneAuthorizeTool(service),
        HarnessEvalSandboxRetryPruneExecuteTool(service),
        HarnessEvalBaselinePromoteTool(service),
        HarnessEvalCompareTool(service),
        HarnessReadKnowledgeTool(service),
        HarnessRunCheckTool(service),
    ]


def _sandbox_retry_progress_callback(
    service: HarnessService,
    *,
    retry_action_id: str,
    event_callback: LegacyEventCallback | None,
) -> SandboxEvalProgressCallback:
    """Project one retry/resume checkpoint through the shared typed protocol."""
    manifest_metadata: tuple[str, tuple[str, ...]] | None = None

    async def publish_progress(
        checkpoint: HarnessSandboxBatchCheckpoint,
    ) -> None:
        nonlocal manifest_metadata
        if event_callback is None:
            return
        if manifest_metadata is None and service.store is not None:
            retry = await service.store.get_sandbox_admission_retry(
                workspace_root=service.workspace_root,
                action_id=retry_action_id,
            )
            if retry is not None and retry.decision == "accepted":
                stored = await service.store.get_sandbox_eval_request(
                    service.workspace_root,
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
                "Sandbox Eval retry/resume 无法恢复进度展示所需的 Request Manifest。",
            )
        await event_callback(
            RuntimeEventType.HARNESS_SANDBOX_EVAL_PROGRESS.value,
            harness_sandbox_eval_progress_payload(
                checkpoint,
                batch_id=manifest_metadata[0],
                check_ids=manifest_metadata[1],
            ),
        )

    return publish_progress


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
            search_hint=("harness explain run failure receipt check evidence why status"),
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
            search_hint=("harness replay deterministic receipt evidence artifact verify history"),
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
            search_hint=("harness eval offline protocol regression fixture suite deterministic"),
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


class HarnessEvalLiveTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_live"

    @property
    def description(self) -> str:
        return "显式执行一次有成本和时限上限的真实模型传输评测"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=True,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=("harness live eval provider model capability cost timeout transport"),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "可选模型；省略时使用 capable/default model",
                },
                "max_duration_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                },
                "max_cost_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 10,
                    "default": 0.05,
                },
                "max_output_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                    "default": 32,
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            receipt = await self._service.eval_live(
                model=kwargs.get("model"),
                max_duration_seconds=kwargs.get("max_duration_seconds", 30.0),
                max_cost_usd=kwargs.get("max_cost_usd", 0.05),
                max_output_tokens=kwargs.get("max_output_tokens", 32),
            )
        except (HarnessLiveEvalError, ValueError) as exc:
            code = getattr(exc, "code", "live_eval_parameters_invalid")
            return f"Harness Live Eval 无法启动（`{code}`）：{exc}"
        return render_harness_live_eval(receipt)


class HarnessEvalLiveBatchTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_live_batch"

    @property
    def description(self) -> str:
        return "重复运行声明式 Live Eval Suite，并保存不可变 H5a 样本"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=True,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=("harness live eval suite repeated provider model h5a baseline"),
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
                    "maximum": 20,
                    "default": 5,
                },
                "batch_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "model": {"type": "string", "minLength": 1, "maxLength": 512},
                "max_total_duration_seconds": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 3_600,
                },
                "max_total_cost_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 10,
                },
            },
            "required": ["suite"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._service.eval_live_batch(
                kwargs.get("suite"),
                repetitions=kwargs.get("repetitions", 5),
                batch_id=kwargs.get("batch_id"),
                model=kwargs.get("model"),
                max_total_duration_seconds=kwargs.get("max_total_duration_seconds"),
                max_total_cost_usd=kwargs.get("max_total_cost_usd"),
            )
        except (HarnessLiveEvalError, ValueError) as exc:
            code = getattr(exc, "code", "live_batch_parameters_invalid")
            return f"Harness Live Eval Batch 无法启动（`{code}`）：{exc}"
        return render_live_batch_status(result)


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
            search_hint=("harness eval baseline comparison receipt regression status history"),
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
            search_hint=("harness sandbox eval profile checks repeated batch h5a validation"),
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
            search_hint=("harness sandbox eval retry resume cancelled batch h5a recovery"),
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
        try:
            receipt = await self._service.retry_sandbox(
                retry_action_id=normalized_action,
                cancel_receipt_id=normalized_cancel,
                cancel_receipt_sha256=normalized_cancel_sha256,
                reason=normalized_reason,
                on_progress=_sandbox_retry_progress_callback(
                    self._service,
                    retry_action_id=normalized_action,
                    event_callback=event_callback,
                ),
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


class HarnessEvalSandboxRetryCatalogTool(_HarnessReadOnlyTool):
    """Inspect durable retry dispatches without claiming or resuming them."""

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retries"

    @property
    def description(self) -> str:
        return "查看当前工作区有界且可校验的 Sandbox retry dispatch 目录"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=("harness sandbox retry dispatch catalog history recovery expired"),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["all", "open", "terminal"],
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 1024,
                    "default": "",
                },
                "assessed_at": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "翻页时复用上一页返回的 ISO 8601 评估时间",
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        state = kwargs.get("state", "all")
        limit = kwargs.get("limit", 20)
        cursor = kwargs.get("cursor", "")
        assessed_at = kwargs.get("assessed_at")
        if (
            not isinstance(state, str)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not isinstance(cursor, str)
            or (assessed_at is not None and not isinstance(assessed_at, str))
        ):
            return (
                "Sandbox retry catalog 参数无效：state、cursor、assessed_at "
                "必须是字符串，limit 必须是整数。"
            )
        try:
            page = await self._service.list_sandbox_retry_dispatches(
                state_filter=state,
                limit=limit,
                cursor=cursor,
                assessed_at=assessed_at,
            )
        except (HarnessSandboxEvalServiceError, HarnessStoreError, ValueError) as exc:
            code = getattr(exc, "code", "sandbox_retry_catalog_unavailable")
            return f"Sandbox retry catalog 暂不可用（`{code}`）：{exc}"
        return render_sandbox_retry_catalog(page)


class HarnessEvalSandboxRetryDetailTool(_HarnessReadOnlyTool):
    """Inspect one exact retry dispatch without changing durable state."""

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retry_detail"

    @property
    def description(self) -> str:
        return "查看一个 Sandbox retry dispatch 的只读权威详情与保护引用"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness sandbox retry dispatch detail receipt ticket recovery retention h5a"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "retry_action_id": {
                    "type": "string",
                    "pattern": r"^hsar_[0-9a-f]{24}$",
                },
                "dispatch_id": {
                    "type": "string",
                    "pattern": r"^hsard_[0-9a-f]{24}$",
                },
                "assessed_at": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "可选的 ISO 8601 评估时间",
                },
            },
            "required": ["retry_action_id", "dispatch_id"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        retry_action_id = kwargs.get("retry_action_id")
        dispatch_id = kwargs.get("dispatch_id")
        assessed_at = kwargs.get("assessed_at")
        if (
            not isinstance(retry_action_id, str)
            or re.fullmatch(r"hsar_[0-9a-f]{24}", retry_action_id) is None
            or not isinstance(dispatch_id, str)
            or re.fullmatch(r"hsard_[0-9a-f]{24}", dispatch_id) is None
            or (assessed_at is not None and not isinstance(assessed_at, str))
        ):
            return (
                "Sandbox retry detail 参数无效：需要合法的 retry_action_id、"
                "dispatch_id 与可选 ISO 8601 assessed_at。"
            )
        try:
            snapshot = await self._service.sandbox_retry_detail(
                retry_action_id=retry_action_id,
                dispatch_id=dispatch_id,
                assessed_at=assessed_at,
            )
        except (HarnessSandboxEvalServiceError, HarnessStoreError, ValueError) as exc:
            code = getattr(exc, "code", "sandbox_retry_detail_unavailable")
            return f"Sandbox retry detail 暂不可用（`{code}`）：{exc}"
        return render_sandbox_retry_detail(snapshot)


class HarnessEvalSandboxRetryRetentionPreviewTool(_HarnessReadOnlyTool):
    """Preview old terminal retry cohorts without deleting durable facts."""

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retry_retention_preview"

    @property
    def description(self) -> str:
        return "预览超过保留期的 Sandbox retry 终态 cohort 与保护引用"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=("harness sandbox retry retention preview terminal prune dry run"),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "retention_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                    "default": 30,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 20,
                },
                "scan_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                },
                "assessed_at": {
                    "type": "string",
                    "maxLength": 64,
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        retention_days = kwargs.get("retention_days", 30)
        limit = kwargs.get("limit", 20)
        scan_limit = kwargs.get("scan_limit", 100)
        assessed_at = kwargs.get("assessed_at")
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(scan_limit, bool)
            or not isinstance(scan_limit, int)
            or (assessed_at is not None and not isinstance(assessed_at, str))
        ):
            return (
                "Sandbox retry retention preview 参数无效：天数、limit 与 "
                "scan_limit 必须是整数，assessed_at 必须是字符串。"
            )
        try:
            preview = await self._service.sandbox_retry_retention_preview(
                retention_days=retention_days,
                limit=limit,
                scan_limit=scan_limit,
                assessed_at=assessed_at,
            )
        except (HarnessSandboxEvalServiceError, HarnessStoreError, ValueError) as exc:
            code = getattr(exc, "code", "sandbox_retry_retention_unavailable")
            return f"Sandbox retry retention preview 暂不可用（`{code}`）：{exc}"
        return render_sandbox_retry_retention_preview(preview)


class HarnessEvalSandboxRetryPruneAuthorizeTool(Tool):
    """Authorize one exact retention candidate without deleting durable facts."""

    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retry_prune_authorize"

    @property
    def description(self) -> str:
        return "重新校验 Sandbox retry retention 候选并签发不可变清理回执"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            requires_persistent_authorization=True,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=("harness sandbox retry retention prune authorize receipt fence"),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "action_id": {
                "type": "string",
                "pattern": r"^hsrpa_[0-9a-f]{24}$",
            },
            "preview_id": {
                "type": "string",
                "pattern": r"^hsrrpv_[0-9a-f]{24}$",
            },
            "preview_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
            "candidate_id": {
                "type": "string",
                "pattern": r"^hsrrp_[0-9a-f]{24}$",
            },
            "candidate_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "retry_action_id": {
                "type": "string",
                "pattern": r"^hsar_[0-9a-f]{24}$",
            },
            "dispatch_id": {
                "type": "string",
                "pattern": r"^hsard_[0-9a-f]{24}$",
            },
            "dispatch_epoch": {"type": "integer", "minimum": 1},
            "dispatch_request_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "dispatch_updated_at": {"type": "string", "maxLength": 64},
            "protection_refs_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "preview_assessed_at": {"type": "string", "maxLength": 64},
            "retention_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3650,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "scan_limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        patterns = {
            "action_id": r"hsrpa_[0-9a-f]{24}",
            "preview_id": r"hsrrpv_[0-9a-f]{24}",
            "preview_sha256": r"[0-9a-f]{64}",
            "candidate_id": r"hsrrp_[0-9a-f]{24}",
            "candidate_sha256": r"[0-9a-f]{64}",
            "retry_action_id": r"hsar_[0-9a-f]{24}",
            "dispatch_id": r"hsard_[0-9a-f]{24}",
            "dispatch_request_sha256": r"[0-9a-f]{64}",
            "protection_refs_sha256": r"[0-9a-f]{64}",
        }
        if any(
            not isinstance(kwargs.get(name), str) or re.fullmatch(pattern, kwargs[name]) is None
            for name, pattern in patterns.items()
        ):
            return "Sandbox retry prune authorize 参数无效：ID 或摘要格式错误。"
        for name in ("retention_days", "limit", "scan_limit", "dispatch_epoch"):
            value = kwargs.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                return f"Sandbox retry prune authorize 参数无效：{name} 必须是整数。"
        if not (
            1 <= kwargs["retention_days"] <= 3650
            and 1 <= kwargs["limit"] <= 20
            and kwargs["limit"] <= kwargs["scan_limit"] <= 100
            and kwargs["dispatch_epoch"] >= 1
        ):
            return "Sandbox retry prune authorize 参数无效：数值超出允许范围。"
        for name, maximum in (
            ("dispatch_updated_at", 64),
            ("preview_assessed_at", 64),
            ("reason", 500),
            ("run_id", 128),
        ):
            value = kwargs.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                return f"Sandbox retry prune authorize 参数无效：{name} 无效。"
        try:
            receipt = await self._service.authorize_sandbox_retry_prune(**kwargs)
        except (HarnessSandboxEvalServiceError, HarnessStoreError, ValueError) as exc:
            code = getattr(exc, "code", "sandbox_retry_prune_unavailable")
            return f"Sandbox retry prune authorize 暂不可用（`{code}`）：{exc}"
        return render_sandbox_retry_prune_receipt(receipt)


class HarnessEvalSandboxRetryPruneExecuteTool(Tool):
    """Atomically consume one accepted prune authorization receipt."""

    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_retry_prune_execute"

    @property
    def description(self) -> str:
        return "原子消费 Sandbox retry prune 授权回执并清理精确安全集"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=True,
            concurrency_safe=True,
            requires_confirmation=True,
            requires_persistent_authorization=True,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=("harness sandbox retry prune execute receipt atomic delete"),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "action_id": {
                "type": "string",
                "pattern": r"^hsrpe_[0-9a-f]{24}$",
            },
            "authorization_action_id": {
                "type": "string",
                "pattern": r"^hsrpa_[0-9a-f]{24}$",
            },
            "authorization_receipt_id": {
                "type": "string",
                "pattern": r"^hsrpr_[0-9a-f]{24}$",
            },
            "authorization_receipt_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "candidate_id": {
                "type": "string",
                "pattern": r"^hsrrp_[0-9a-f]{24}$",
            },
            "candidate_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "retry_action_id": {
                "type": "string",
                "pattern": r"^hsar_[0-9a-f]{24}$",
            },
            "dispatch_id": {
                "type": "string",
                "pattern": r"^hsard_[0-9a-f]{24}$",
            },
            "protection_refs_sha256": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        patterns = {
            "action_id": r"hsrpe_[0-9a-f]{24}",
            "authorization_action_id": r"hsrpa_[0-9a-f]{24}",
            "authorization_receipt_id": r"hsrpr_[0-9a-f]{24}",
            "authorization_receipt_sha256": r"[0-9a-f]{64}",
            "candidate_id": r"hsrrp_[0-9a-f]{24}",
            "candidate_sha256": r"[0-9a-f]{64}",
            "retry_action_id": r"hsar_[0-9a-f]{24}",
            "dispatch_id": r"hsard_[0-9a-f]{24}",
            "protection_refs_sha256": r"[0-9a-f]{64}",
        }
        if any(
            not isinstance(kwargs.get(name), str) or re.fullmatch(pattern, kwargs[name]) is None
            for name, pattern in patterns.items()
        ):
            return "Sandbox retry prune execute 参数无效：ID 或摘要格式错误。"
        for name, maximum in (("reason", 500), ("run_id", 128)):
            value = kwargs.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                return f"Sandbox retry prune execute 参数无效：{name} 无效。"
        try:
            receipt = await self._service.execute_sandbox_retry_prune(**kwargs)
        except (HarnessSandboxEvalServiceError, HarnessStoreError, ValueError) as exc:
            code = getattr(
                exc,
                "code",
                "sandbox_retry_prune_execution_unavailable",
            )
            return f"Sandbox retry prune execute 暂不可用（`{code}`）：{exc}"
        return render_sandbox_retry_prune_execution_receipt(receipt)


class HarnessEvalSandboxResumeTool(Tool):
    """Resume one existing durable retry dispatch without a new retry intent."""

    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_eval_sandbox_resume"

    @property
    def description(self) -> str:
        return "使用既有 retry receipt 与 dispatch fence 恢复中断的 Sandbox Eval"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint=("harness sandbox retry resume dispatch expired crash recovery h5a"),
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
                "dispatch_id": {
                    "type": "string",
                    "pattern": "^hsard_[0-9a-f]{24}$",
                },
                "retry_receipt_id": {
                    "type": "string",
                    "pattern": "^hsarr_[0-9a-f]{24}$",
                },
                "retry_receipt_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
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
                "dispatch_id",
                "retry_receipt_id",
                "retry_receipt_sha256",
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
        dispatch_id = kwargs.get("dispatch_id")
        retry_receipt_id = kwargs.get("retry_receipt_id")
        retry_receipt_sha256 = kwargs.get("retry_receipt_sha256")
        run_id = kwargs.get("run_id")
        values = (
            retry_action_id,
            dispatch_id,
            retry_receipt_id,
            retry_receipt_sha256,
            run_id,
        )
        if (
            any(not isinstance(item, str) for item in values)
            or re.fullmatch(r"hsar_[0-9a-f]{24}", retry_action_id) is None
            or re.fullmatch(r"hsard_[0-9a-f]{24}", dispatch_id) is None
            or re.fullmatch(r"hsarr_[0-9a-f]{24}", retry_receipt_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", retry_receipt_sha256) is None
            or run_id != run_id.strip()
            or not run_id
            or len(run_id) > 128
        ):
            return (
                "Harness Sandbox Eval resume 参数无效："
                "action、dispatch、retry receipt、SHA-256 与 run_id 均必须精确提供。"
            )
        try:
            receipt = await self._service.resume_sandbox_retry(
                retry_action_id=retry_action_id,
                dispatch_id=dispatch_id,
                retry_receipt_id=retry_receipt_id,
                retry_receipt_sha256=retry_receipt_sha256,
                on_progress=_sandbox_retry_progress_callback(
                    self._service,
                    retry_action_id=retry_action_id,
                    event_callback=event_callback,
                ),
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
            code = getattr(exc, "code", "sandbox_eval_resume_infrastructure_error")
            return f"Harness Sandbox Eval resume 未完成（`{code}`）：{exc}"
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
                "harness repository knowledge read source docs instructions symbol path evidence"
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
