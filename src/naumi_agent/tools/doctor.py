"""Doctor diagnostics tool."""

from __future__ import annotations

from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.doctor import render_doctor_report, run_doctor
from naumi_agent.ui.doctor_export import (
    DoctorExportPlan,
    build_doctor_export_plan,
    render_doctor_export_preview,
    render_doctor_export_receipt,
    write_doctor_export,
)
from naumi_agent.ui.doctor_health import build_doctor_health_snapshot
from naumi_agent.ui.doctor_probe import (
    DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS,
    DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS,
    DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS,
    render_doctor_live_probe_result,
    run_bounded_doctor_live_probe,
)


class DoctorDiagnosticsTool(Tool):
    """Run local environment diagnostics through the shared doctor screen."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "doctor_diagnostics"

    @property
    def description(self) -> str:
        return (
            "诊断 NaumiAgent 本机运行环境，检查 Python、配置、API key、git、"
            "rg、docker、MCP 和浏览器 daemon。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="环境诊断",
            search_hint=(
                "doctor diagnostics environment python config api key git rg "
                "docker mcp browser daemon"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        report = await run_doctor(
            self._engine._config,
            workspace_root=self._engine.workspace_root,
            mcp_manager=self._engine._mcp_manager,
            model_router=self._engine.router,
        )
        return render_doctor_report(report)


class DoctorExportTool(Tool):
    """Preview and export the same bounded local Doctor facts."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._previewed_plan: DoctorExportPlan | None = None

    @property
    def name(self) -> str:
        return "doctor_export_diagnostics"

    @property
    def description(self) -> str:
        return (
            "先预览再导出脱敏诊断 ZIP。包内只有 typed Health、manifest 和说明，"
            "不会包含聊天、reasoning、原始 trace、环境变量全集、凭据或源码。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=False,
            requires_confirmation=False,
            path_argument_names=(),
            user_facing_name="导出脱敏诊断包",
            search_hint=(
                "doctor export diagnostics bundle manifest redacted health support"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["preview", "write"],
                    "description": "preview 不写文件；write 需要上一轮来源快照摘要。",
                },
                "expected_snapshot_sha256": {
                    "type": "string",
                    "description": "write 时必须等于 preview 返回的来源快照 SHA-256。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        action: str,
        expected_snapshot_sha256: str = "",
        **kwargs: Any,
    ) -> str:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"preview", "write"}:
            raise ValueError("action 必须是 preview 或 write。")
        expected = str(expected_snapshot_sha256 or "").strip().lower()
        if normalized_action == "preview" and expected:
            raise ValueError("preview 不能携带 expected_snapshot_sha256。")
        if normalized_action == "write":
            if self._previewed_plan is None:
                raise ValueError("缺少本进程内的诊断包 preview，拒绝写入。")
            if expected != self._previewed_plan.preview.source_snapshot_sha256:
                raise ValueError("诊断 preview 摘要不匹配，拒绝写入。")

        report = await run_doctor(
            self._engine._config,
            workspace_root=self._engine.workspace_root,
            mcp_manager=self._engine._mcp_manager,
            model_router=self._engine.router,
        )
        snapshot = build_doctor_health_snapshot(report)
        if normalized_action == "preview":
            plan = build_doctor_export_plan(
                snapshot,
                workspace_root=self._engine.workspace_root,
            )
            self._previewed_plan = plan
            return render_doctor_export_preview(plan.preview)
        if expected != snapshot.snapshot_sha256:
            self._previewed_plan = None
            raise ValueError("诊断事实已变化或缺少精确 preview 摘要，拒绝写入。")
        previewed_plan = self._previewed_plan
        self._previewed_plan = None
        return render_doctor_export_receipt(write_doctor_export(previewed_plan))


class DoctorLiveProbeTool(Tool):
    """Run one explicit, bounded provider connectivity request."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "doctor_live_probe"

    @property
    def description(self) -> str:
        return (
            "显式验证模型提供商连接：最多发送 1 个请求、最多生成 8 个输出 token、"
            "默认 15 秒超时且不会自动重试。仅在确实需要真实连通性证据时调用。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=False,
            requires_confirmation=False,
            user_facing_name="模型提供商在线探测",
            search_hint=(
                "doctor live provider probe connectivity authentication endpoint "
                "timeout bounded request"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "timeout_ms": {
                    "type": "integer",
                    "minimum": DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS,
                    "maximum": DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS,
                    "default": DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS,
                    "description": "单次模型请求超时，范围 1000..60000 毫秒。",
                }
            },
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        timeout_ms: int = DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS,
        **kwargs: Any,
    ) -> str:
        result = await run_bounded_doctor_live_probe(
            self._engine._config,
            workspace_root=self._engine.workspace_root,
            timeout_ms=timeout_ms,
            mcp_manager=getattr(self._engine, "_mcp_manager", None),
            model_router=self._engine.router,
        )
        return render_doctor_live_probe_result(result)
