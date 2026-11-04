"""Shared personal-agent operations over the catalog and real session store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from naumi_agent.memory.session import Session
from naumi_agent.model.router import ModelTier
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError
from naumi_agent.personal.store import PersonalAgentStore

logger = logging.getLogger(__name__)

_TOOL_LABELS = {
    "personal_memories": "检索当前 Agent 的已确认记忆并提出候选",
    "memory_store": "提出当前 Agent 的待确认记忆候选",
    "memory_recall": "检索当前 Agent 范围内的已确认记忆",
    "personal_items": "安排、查询和控制当前 Agent 范围内的个人事项",
    "personal_artifacts": "读取、创建、修订与分享当前 Agent 的资料和产物",
    "glob": "按文件名查找文件",
    "grep": "搜索文件内容",
    "read": "读取文件",
    "file_read": "读取文件",
    "file_write": "写入文件",
    "file_edit": "编辑文件",
    "bash_run": "执行终端命令",
    "code_execute": "运行代码",
    "web_search": "搜索网络资料",
    "web_fetch": "读取网页内容",
    "request_user_input": "向用户提问",
    "output_publish": "交付文件与图片",
    "yaml_validate": "检查 YAML 格式",
    "yaml_micro_verify": "验证 YAML 配置",
    "browser_goto": "打开网页",
    "browser_observe": "读取页面状态",
    "browser_click": "点击页面元素",
    "browser_type": "输入文本",
    "browser_hover": "悬停页面元素",
    "browser_keypress": "发送按键",
    "browser_scroll": "滚动页面",
    "browser_evaluate": "执行页面脚本",
    "browser_select_option": "选择表单选项",
    "browser_handle_dialog": "处理页面对话框",
    "browser_navigate_back": "返回上一页",
    "browser_tabs": "查看浏览器标签页",
    "browser_switch_tab": "切换标签页",
    "browser_waitFor": "等待页面就绪",
    "browser_upload": "上传文件",
    "browser_drag": "拖动页面元素",
    "browser_drag_file": "拖入文件",
    "browser_screenshot": "截取页面图片",
    "browser_debug_state": "查看浏览器诊断状态",
    "browser_text_layout_audit": "检查文字布局",
    "browser_cdp_health": "检查浏览器调试通道",
    "browser_attach_diagnostics": "诊断浏览器连接",
    "browser_chrome_launcher_info": "查看浏览器启动信息",
    "browser_start": "启动浏览器",
    "browser_stop": "关闭浏览器",
    "personal_message": "向协作范围内的Agent询问并接收结果",
    "personal_team": "协调当前团队成员并提交真实分工计划",
}

# Explicit leaf capabilities prevent future registry additions from silently granting
# catalog, global-memory, delegation, or code-generation/registration authority.
ASSIGNABLE_TOOL_NAMES = frozenset(
    {
        "personal_memories",
        "memory_store",
        "memory_recall",
        "personal_items",
        "personal_artifacts",
        "glob",
        "grep",
        "file_read",
        "read",
        "file_write",
        "file_edit",
        "bash_run",
        "code_execute",
        "web_search",
        "web_fetch",
        "request_user_input",
        "personal_message",
        "personal_team",
        "output_publish",
        "yaml_validate",
        "yaml_micro_verify",
        "browser_goto",
        "browser_observe",
        "browser_click",
        "browser_type",
        "browser_hover",
        "browser_keypress",
        "browser_scroll",
        "browser_evaluate",
        "browser_select_option",
        "browser_handle_dialog",
        "browser_navigate_back",
        "browser_tabs",
        "browser_switch_tab",
        "browser_waitFor",
        "browser_upload",
        "browser_drag",
        "browser_drag_file",
        "browser_screenshot",
        "browser_debug_state",
        "browser_text_layout_audit",
        "browser_cdp_health",
        "browser_attach_diagnostics",
        "browser_chrome_launcher_info",
        "browser_start",
        "browser_stop",
    }
)


def _absolute_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


class PersonalAgentService:
    """One service shared by manual API/commands and the agent-facing tool.

    Profile methods are synchronous. ``open_session`` is async and never changes
    the Engine's active session, messages, model selection, or runtime mode.
    """

    def __init__(self, engine: Any, store: PersonalAgentStore) -> None:
        self.engine = engine
        self.store = store
        config = getattr(engine, "config", None) or getattr(engine, "_config", None)
        if config is None or not getattr(config.memory, "session_db_path", ""):
            raise PersonalAgentError("configuration_error", "Agent 服务缺少会话资料库配置")
        self.workspace_root = str(Path(engine.workspace_root).expanduser().resolve())
        database_path = _absolute_key(config.memory.session_db_path)
        self.session_db_key = hashlib.sha256(database_path.encode("utf-8")).hexdigest()
        identity = _absolute_key(self.workspace_root) + "\0" + database_path
        self.workspace_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self._session_lock = asyncio.Lock()

    def assignable_tools(self) -> list[dict[str, Any]]:
        tools = []
        for tool in self.engine.tool_registry.all():
            if tool.name not in ASSIGNABLE_TOOL_NAMES or tool.metadata.delegated_tool_names:
                continue
            if tool.name in {"personal_memories", "memory_store", "memory_recall"}:
                from naumi_agent.personal.memory_tools import (
                    MemoryRecallAlias,
                    MemoryStoreAlias,
                    PersonalMemoryTool,
                )

                if type(tool) not in {PersonalMemoryTool, MemoryStoreAlias, MemoryRecallAlias} or (
                    tool.service is not getattr(self.engine, "personal_memory", None)
                    or tool.engine is not self.engine
                ):
                    continue
            description = tool.description
            if not re.search(r"[\u4e00-\u9fff]", description):
                label = tool.user_facing_name
                description = (
                    label
                    if re.search(r"[\u4e00-\u9fff]", label)
                    else _TOOL_LABELS.get(tool.name, tool.name)
                )
            entry = {
                "name": tool.name,
                "description": description,
                "read_only": bool(tool.metadata.read_only),
            }
            if tool.name == "personal_team":
                entry["kinds"] = ["team"]
            tools.append(entry)
        return sorted(tools, key=lambda item: item["name"])

    def validate_tools(self, names: Any) -> tuple[str, ...]:
        if not isinstance(names, (tuple, list)) or len(names) > 128:
            raise PersonalAgentError("invalid_input", "工具配置必须是最多 128 项的列表")
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise PersonalAgentError("invalid_input", "工具列表包含无效名称或重复项目")
        allowed = {tool["name"] for tool in self.assignable_tools()}
        if any(name not in allowed for name in names):
            raise PersonalAgentError("tool_not_assignable", "包含未注册或暂不支持分配给Agent的工具")
        return tuple(sorted(names))

    def list(self, *, include_archived: bool = False) -> tuple[PersonalAgent, ...]:
        return self.store.list(include_archived=include_archived)

    def get(self, agent_id: str) -> PersonalAgent:
        agent = self.store.get(agent_id)
        if agent is None:
            raise PersonalAgentError("not_found", "该 Agent 不存在，请刷新列表")
        return agent

    def create(self, **fields: Any) -> PersonalAgent:
        allowed = {
            "name",
            "instructions",
            "request_id",
            "description",
            "model_tier",
            "tool_names",
            "kind",
            "member_ids",
        }
        if set(fields) - allowed or not {"name", "instructions", "request_id"} <= set(fields):
            raise PersonalAgentError("invalid_input", "Agent 资料缺少必填字段或包含不支持的字段")
        fields["tool_names"] = self.validate_tools(fields.get("tool_names", ()))
        if "personal_team" in fields["tool_names"] and fields.get("kind", "specialist") != "team":
            raise PersonalAgentError("tool_not_assignable", "团队协调工具只能分配给团队")
        return self.store.create(**fields)

    def update(self, agent_id: str, expected_version: int, **fields: Any) -> PersonalAgent:
        if "tool_names" in fields:
            fields["tool_names"] = self.validate_tools(fields["tool_names"])
            if "personal_team" in fields["tool_names"] and self.get(agent_id).kind != "team":
                raise PersonalAgentError("tool_not_assignable", "团队协调工具只能分配给团队")
        return self.store.update(agent_id, expected_version, **fields)

    def history(self, agent_id: str) -> tuple[PersonalAgent, ...]:
        return self.store.history(agent_id)

    def current(self, session_id: str) -> PersonalAgent | None:
        return self.store.agent_for_session(
            self.workspace_key, session_id, session_db_key=self.session_db_key
        )

    def require_active_profile(self, agent_id: str) -> PersonalAgent:
        agent = self.get(agent_id)
        if agent.status != "active":
            raise PersonalAgentError("agent_inactive", "Agent 已停用或归档，请启用后继续")
        self.validate_tools(agent.tool_names)
        return agent

    def _executable(self, agent_id: str) -> PersonalAgent:
        agent = self.require_active_profile(agent_id)
        if agent.kind == "team":
            raise PersonalAgentError(
                "team_turn_required", "请通过团队对话发起任务，不将团队当作单个Agent执行"
            )
        return agent

    def _session_valid(self, session: Session) -> bool:
        if (
            session.engine != "naumi"
            or not session.workspace_root
            or (_absolute_key(session.workspace_root) != _absolute_key(self.workspace_root))
        ):
            raise PersonalAgentError("session_scope_mismatch", "Agent 会话属于其他工作区或执行引擎")
        return session.status == "active" and session.archived_at is None

    async def _discard_unbound(self, session: Session) -> None:
        # Only this call's freshly minted session may be deleted. Historical ownership
        # prevents deleting a winner even if another opener already replaced its pointer.
        if self.current(session.id) is None:
            await self.engine.session_store.delete(session.id)

    async def open_session(self, agent_id: str) -> dict[str, Any]:
        try:
            return await self._open_session(agent_id)
        except PersonalAgentError:
            raise
        except Exception as exc:
            logger.warning("Personal-agent session admission failed: %s", type(exc).__name__)
            raise PersonalAgentError(
                "storage_error", "Agent 会话暂时未能打开，请检查服务状态后重试"
            ) from exc

    async def _open_session(self, agent_id: str) -> dict[str, Any]:
        async with self._session_lock:
            for _ in range(3):
                profile = self.require_active_profile(agent_id)
                binding = self.store.get_binding(agent_id, self.workspace_key)
                if binding is not None:
                    if binding.session_db_key != self.session_db_key:
                        raise PersonalAgentError(
                            "session_scope_mismatch", "Agent 会话资料库标识不一致"
                        )
                    existing = await self.engine.session_store.load(binding.session_id)
                    if existing is not None and self._session_valid(existing):
                        marker = getattr(existing, "personal_agent_id", "")
                        if marker and marker != agent_id:
                            raise PersonalAgentError(
                                "session_scope_mismatch", "会话已属于其他Agent，已停止打开"
                            )
                        if not marker:
                            existing.personal_agent_id = agent_id
                            await self.engine.session_store.save(existing)
                        if profile.kind == "team" and not getattr(existing, "personal_team_id", ""):
                            await self.engine.session_store.bind_personal_team(
                                existing.id, agent_id
                            )
                        current = self.require_active_profile(agent_id)
                        return {
                            "agent_id": agent_id,
                            "session_id": existing.id,
                            "version": current.version,
                        }
                try:
                    model = self.engine.router.resolve_model(ModelTier(profile.model_tier))
                except Exception as exc:
                    raise PersonalAgentError(
                        "model_unavailable", "该 Agent 的模型尚未配置，请先选择模型"
                    ) from exc
                if not isinstance(model, str) or not model.strip():
                    raise PersonalAgentError(
                        "model_unavailable", "该 Agent 的模型尚未配置，请先选择模型"
                    )
                session = Session(
                    title=profile.name,
                    personal_agent_id=profile.id,
                    personal_team_id=profile.id if profile.kind == "team" else "",
                    model=model,
                    engine="naumi",
                    workspace_root=self.workspace_root,
                )
                try:
                    await self.engine.session_store.save(session)
                    selected = self.store.bind_session(
                        agent_id,
                        self.workspace_key,
                        session.id,
                        session_db_key=self.session_db_key,
                        expected_session_id=binding.session_id if binding is not None else None,
                    )
                except BaseException as exc:
                    try:
                        await asyncio.shield(self._discard_unbound(session))
                    except Exception:
                        logger.warning("Could not remove an unbound personal-agent session")
                    if isinstance(exc, PersonalAgentError) and exc.code == "binding_conflict":
                        continue
                    raise
                if selected.session_id != session.id:
                    await self._discard_unbound(session)
                    continue
                current = self.require_active_profile(agent_id)
                return {"agent_id": agent_id, "session_id": session.id, "version": current.version}
            raise PersonalAgentError("binding_conflict", "Agent 会话正在恢复，请稍后重新打开")
