"""Agent tools for durable workspace goals."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.orchestrator.goal_store import (
    GoalStatus,
    GoalStore,
    GoalStoreError,
    format_goal,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.orchestrator.pursuit_terminal_outbox_worker import (
    PursuitTerminalOutboxWorkerSnapshot,
)
from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.goal_panel import (
    build_goal_pursuit_snapshot_with_recovery,
    render_goal_interaction_detail,
    render_goal_pursuit_snapshot,
)
from naumi_agent.ui.pursuit_recovery import PursuitRecoveryAuthority

_RUN_ID_RE = re.compile(r"run_id:\s*`([A-Za-z0-9_.:-]{1,128})`")


class GoalCreateTool(Tool):
    def __init__(self, store: GoalStore, session_id_getter: Callable[[], str]) -> None:
        self._store = store
        self._session_id_getter = session_id_getter

    @property
    def name(self) -> str:
        return "goal_create"

    @property
    def description(self) -> str:
        return (
            "创建一个跨轮次持久目标。只有用户明确要求使用 /goal 或明确要求保存长期目标时"
            "才能调用；不要把普通任务自动升级为持久目标。一个工作区只能有一个未完成目标。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="创建持久目标",
            search_hint="goal create durable workspace objective explicit user request",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "要持续推进的目标"},
            },
            "required": ["objective"],
        }

    async def execute(self, *, objective: str, **kwargs: Any) -> str:
        try:
            goal = self._store.create(
                objective,
                session_id=self._session_id_getter() or "",
            )
        except GoalStoreError as exc:
            return f"⚠️ 目标输入无效：{exc}"
        return "✅ 目标已创建。后续轮次会自动看到它。\n\n" + format_goal(goal)


class GoalStatusTool(Tool):
    def __init__(
        self,
        store: GoalStore,
        pursuit_store: PursuitStore,
        *,
        recovery_authority: PursuitRecoveryAuthority | None = None,
        workspace_root: str | Path | None = None,
        terminal_outbox_enabled: bool | None = None,
        terminal_outbox_worker_snapshot: (
            Callable[[], PursuitTerminalOutboxWorkerSnapshot] | None
        ) = None,
    ) -> None:
        self._store = store
        self._pursuit_store = pursuit_store
        self._recovery_authority = recovery_authority
        self._workspace_root = Path(
            workspace_root or store.base_dir.parent
        ).expanduser().resolve()
        self._terminal_outbox_enabled = terminal_outbox_enabled
        self._terminal_outbox_worker_snapshot = terminal_outbox_worker_snapshot

    @property
    def name(self) -> str:
        return "goal_status"

    @property
    def description(self) -> str:
        return "查看当前未完成目标，或按目标 ID 查看历史目标。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="查看持久目标",
            search_hint="goal status current durable objective",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "目标 ID；省略时读取当前目标"},
            },
            "required": [],
        }

    async def execute(self, *, goal_id: str = "", **kwargs: Any) -> str:
        try:
            goal = self._store.get(goal_id) if goal_id else self._store.current()
        except GoalStoreError as exc:
            return f"⚠️ 目标 ID 无效：{exc}"
        if goal is None:
            if goal_id:
                return f"目标不存在：{goal_id}"
            return render_goal_pursuit_snapshot(
                await build_goal_pursuit_snapshot_with_recovery(
                    self._store,
                    self._pursuit_store,
                    self._recovery_authority,
                    workspace_root=self._workspace_root,
                    limit=1,
                    include_finished=False,
                    terminal_outbox_enabled=self._terminal_outbox_enabled,
                    terminal_outbox_worker_snapshot=(
                        self._terminal_outbox_worker_snapshot
                    ),
                )
            )
        return render_goal_pursuit_snapshot(
            await build_goal_pursuit_snapshot_with_recovery(
                self._store,
                self._pursuit_store,
                self._recovery_authority,
                workspace_root=self._workspace_root,
                limit=1,
                include_finished=bool(goal_id),
                selected_goal_id=goal_id,
                terminal_outbox_enabled=self._terminal_outbox_enabled,
                terminal_outbox_worker_snapshot=(
                    self._terminal_outbox_worker_snapshot
                ),
            )
        )


class GoalListTool(Tool):
    def __init__(
        self,
        store: GoalStore,
        pursuit_store: PursuitStore,
        *,
        recovery_authority: PursuitRecoveryAuthority | None = None,
        workspace_root: str | Path | None = None,
        terminal_outbox_enabled: bool | None = None,
        terminal_outbox_worker_snapshot: (
            Callable[[], PursuitTerminalOutboxWorkerSnapshot] | None
        ) = None,
    ) -> None:
        self._store = store
        self._pursuit_store = pursuit_store
        self._recovery_authority = recovery_authority
        self._workspace_root = Path(
            workspace_root or store.base_dir.parent
        ).expanduser().resolve()
        self._terminal_outbox_enabled = terminal_outbox_enabled
        self._terminal_outbox_worker_snapshot = terminal_outbox_worker_snapshot

    @property
    def name(self) -> str:
        return "goal_list"

    @property
    def description(self) -> str:
        return "列出工作区的持久目标记录。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="持久目标列表",
            search_hint="goal list history active completed",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_finished": {
                    "type": "boolean",
                    "description": "是否包含已完成和已取消目标",
                    "default": True,
                },
                "interaction_filter": {
                    "type": "string",
                    "enum": ["all", "pending", "answered", "expired", "cancelled"],
                    "description": "持久用户交互状态筛选",
                    "default": "all",
                },
                "interaction_cursor": {
                    "type": "string",
                    "description": "上一页返回的不透明交互 cursor",
                    "default": "",
                },
                "selected_interaction_id": {
                    "type": "string",
                    "description": "可选的页内交互详情 ID",
                    "default": "",
                },
                "selected_goal_id": {
                    "type": "string",
                    "description": "可选的历史目标详情 ID",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        include_finished: bool = True,
        interaction_filter: str = "all",
        interaction_cursor: str = "",
        selected_interaction_id: str = "",
        selected_goal_id: str = "",
        **kwargs: Any,
    ) -> str:
        return render_goal_pursuit_snapshot(
            await build_goal_pursuit_snapshot_with_recovery(
                self._store,
                self._pursuit_store,
                self._recovery_authority,
                workspace_root=self._workspace_root,
                limit=50,
                include_finished=include_finished,
                selected_goal_id=selected_goal_id,
                interaction_limit=10,
                interaction_filter=interaction_filter,
                interaction_cursor=interaction_cursor,
                selected_interaction_id=selected_interaction_id,
                terminal_outbox_enabled=self._terminal_outbox_enabled,
                terminal_outbox_worker_snapshot=(
                    self._terminal_outbox_worker_snapshot
                ),
            )
        )


class GoalUpdateTool(Tool):
    def __init__(self, store: GoalStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "goal_update"

    @property
    def description(self) -> str:
        return "更新当前持久目标的生命周期状态：暂停、恢复、阻塞、完成或取消。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="更新持久目标",
            search_hint="goal update pause resume block complete cancel",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [item.value for item in GoalStatus],
                    "description": "目标状态",
                },
                "note": {"type": "string", "description": "状态变更说明"},
                "goal_id": {"type": "string", "description": "省略时更新当前目标"},
            },
            "required": ["status"],
        }

    async def execute(
        self,
        *,
        status: str,
        note: str = "",
        goal_id: str = "",
        **kwargs: Any,
    ) -> str:
        try:
            target = GoalStatus(status)
        except ValueError:
            choices = ", ".join(item.value for item in GoalStatus)
            return f"⚠️ 不支持的目标状态：{status}。可选：{choices}。"
        try:
            current = self._store.get(goal_id) if goal_id else self._store.current()
            if current is None:
                return "当前没有未完成目标。使用 `/goal <目标>` 创建。"
            updated = self._store.update(current.id, target, note=note)
        except GoalStoreError as exc:
            return f"⚠️ 目标状态更新失败：{exc}"
        label = {
            GoalStatus.ACTIVE: "已恢复",
            GoalStatus.PAUSED: "已暂停",
            GoalStatus.BLOCKED: "已标记阻塞",
            GoalStatus.COMPLETED: "已完成",
            GoalStatus.CANCELLED: "已取消",
        }[updated.status]
        return f"✅ 目标{label}。\n\n{format_goal(updated)}"


class _GoalInteractionTool(Tool):
    def __init__(
        self,
        store: GoalStore,
        interaction_authority: Any,
        *,
        workspace_root: str | Path,
    ) -> None:
        self._store = store
        self._authority = interaction_authority
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    async def _read_linked_record(
        self,
        interaction_id: str,
    ) -> tuple[HarnessInteractionRecord | None, str]:
        if not re.fullmatch(r"ask-[A-Za-z0-9._:-]{1,128}", interaction_id):
            return None, "⚠️ interaction_id 格式无效。"
        if self._authority is None:
            return None, "⚠️ 持久交互 authority 不可用。"
        try:
            record = await self._authority.get_interaction(
                workspace_root=self._workspace_root,
                interaction_id=interaction_id,
            )
        except Exception:
            return None, "⚠️ 持久交互读取失败，请运行 `/doctor`。"
        if record is None:
            return None, f"⚠️ 用户交互不存在：`{interaction_id}`。"
        try:
            linked_runs = {
                goal.pursuit_run_id
                for goal in self._store.list(include_finished=True, limit=50)
                if goal.pursuit_run_id
            }
        except GoalStoreError:
            return None, "⚠️ Goal 状态读取失败，请运行 `/doctor`。"
        if record.subject_kind != "pursuit" or record.subject_id not in linked_runs:
            return None, "⚠️ 该交互不属于当前 Goal 页面中的 Pursuit。"
        return record, ""


class GoalInteractionDetailTool(_GoalInteractionTool):
    """Inspect one Goal-linked durable interaction without changing authority."""

    @property
    def name(self) -> str:
        return "goal_interaction_detail"

    @property
    def description(self) -> str:
        return "查看 Goal/Pursuit 持久用户交互的选项、答案、时序和租约状态。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="查看 Goal 用户交互详情",
            search_hint="goal pursuit interaction detail answer lease takeover eligibility",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "pattern": r"^ask-[A-Za-z0-9._:-]{1,128}$",
                    "description": "Goal 页面展示的持久交互 ID",
                },
            },
            "required": ["interaction_id"],
        }

    async def execute(self, *, interaction_id: str, **kwargs: Any) -> str:
        record, error = await self._read_linked_record(interaction_id)
        if record is None:
            return error
        return render_goal_interaction_detail(record)


class GoalInteractionCancelTool(_GoalInteractionTool):
    """Cancel one pending Pursuit interaction through Harness authority."""

    @property
    def name(self) -> str:
        return "goal_interaction_cancel"

    @property
    def description(self) -> str:
        return "取消 Goal/Pursuit 正在等待的持久用户交互。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="取消 Goal 用户交互",
            search_hint="goal pursuit interaction cancel pending user request",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "pattern": r"^ask-[A-Za-z0-9._:-]{1,128}$",
                    "description": "Goal 页面展示的持久交互 ID",
                },
            },
            "required": ["interaction_id"],
        }

    async def execute(self, *, interaction_id: str, **kwargs: Any) -> str:
        record, error = await self._read_linked_record(interaction_id)
        if record is None:
            return error
        if record.state != "pending":
            return f"ℹ️ 用户交互已是终态：{record.state}。"
        try:
            cancelled = await self._authority.cancel_interaction(
                workspace_root=self._workspace_root,
                interaction_id=record.interaction_id,
                expected_sequence=record.sequence,
                now=datetime.now(UTC).isoformat(),
            )
        except Exception:
            return "⚠️ 用户交互在取消前已变化，请刷新 Goal 页面重试。"
        return f"✅ 已取消用户交互 `{cancelled.interaction_id}`。"


class GoalPursueTool(Tool):
    def __init__(
        self,
        store: GoalStore,
        pursuit_tool_getter: Callable[[], Tool | None],
    ) -> None:
        self._store = store
        self._pursuit_tool_getter = pursuit_tool_getter

    @property
    def name(self) -> str:
        return "goal_pursue"

    @property
    def description(self) -> str:
        return "使用当前 active 持久目标启动既有 Pursuit 自主循环，并关联运行 ID。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="自主追踪当前目标",
            search_hint="goal pursue autonomous loop current durable objective",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        goal = self._store.current()
        if goal is None:
            return "当前没有未完成目标。使用 `/goal <目标>` 创建。"
        if goal.status is not GoalStatus.ACTIVE:
            return f"当前目标为 {goal.status.value}，请先 `/goal resume` 再启动 Pursuit。"
        pursuit = self._pursuit_tool_getter()
        if pursuit is None:
            return "⚠️ 目标追踪工具未注册，无法启动自主循环。"
        result = await pursuit.execute(goal=goal.objective)
        match = _RUN_ID_RE.search(result)
        if match is None:
            return result + "\n\n⚠️ Pursuit 未返回有效 run_id，目标未建立运行关联。"
        linked = self._store.attach_pursuit(goal.id, match.group(1))
        return result + f"\n\n已关联持久目标 `{linked.id}`。"


def create_goal_tools(
    store: GoalStore,
    pursuit_store: PursuitStore,
    *,
    session_id_getter: Callable[[], str],
    pursuit_tool_getter: Callable[[], Tool | None],
    recovery_authority: PursuitRecoveryAuthority | None = None,
    workspace_root: str | Path | None = None,
    terminal_outbox_enabled: bool | None = None,
    terminal_outbox_worker_snapshot: (
        Callable[[], PursuitTerminalOutboxWorkerSnapshot] | None
    ) = None,
) -> list[Tool]:
    return [
        GoalCreateTool(store, session_id_getter),
        GoalStatusTool(
            store,
            pursuit_store,
            recovery_authority=recovery_authority,
            workspace_root=workspace_root,
            terminal_outbox_enabled=terminal_outbox_enabled,
            terminal_outbox_worker_snapshot=terminal_outbox_worker_snapshot,
        ),
        GoalListTool(
            store,
            pursuit_store,
            recovery_authority=recovery_authority,
            workspace_root=workspace_root,
            terminal_outbox_enabled=terminal_outbox_enabled,
            terminal_outbox_worker_snapshot=terminal_outbox_worker_snapshot,
        ),
        GoalUpdateTool(store),
        GoalInteractionDetailTool(
            store,
            recovery_authority,
            workspace_root=workspace_root or store.base_dir.parent,
        ),
        GoalInteractionCancelTool(
            store,
            recovery_authority,
            workspace_root=workspace_root or store.base_dir.parent,
        ),
        GoalPursueTool(store, pursuit_tool_getter),
    ]
