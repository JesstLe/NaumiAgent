"""Skill → Tool 桥接.

将已加载的 Skill 注册为 Agent 可调用的 Tool，
使 LLM 可以在推理链中自主决策何时使用某个 Skill。
"""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.skills.skill import Skill
from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

MAX_SKILL_ARGUMENT_CHARS = 8_000
MAX_RENDERED_SKILL_CHARS = 80_000


class SkillTool(Tool):
    """将一个 Skill 包装为 Tool，供 LLM 调用.

    LLM 调用 skill_execute(name="code-review", arguments="src/") 时，
    会渲染 Skill 的指令模板并将结果作为 tool response 返回。
    """

    def __init__(self, skill: Skill) -> None:
        self._skill = skill

    @property
    def name(self) -> str:
        return f"skill_{self._skill.name}"

    @property
    def description(self) -> str:
        desc = self._skill.description or f"Execute skill: {self._skill.name}"
        if self._skill.arguments:
            args_desc = ", ".join(
                f"{a.name}({'required' if a.required else 'optional'})"
                for a in self._skill.arguments
            )
            desc += f" Arguments: {args_desc}."
        return desc

    @property
    def metadata(self) -> ToolMetadata:
        dynamic_context = "`!`" in self._skill.instructions
        return ToolMetadata(
            read_only=not dynamic_context,
            destructive=dynamic_context,
            concurrency_safe=not dynamic_context,
            requires_confirmation=True if dynamic_context else False,
            user_facing_name=f"Skill: {self._skill.name}",
            search_hint=f"skill render instructions {self._skill.name}",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "arguments": {
                "type": "string",
                "description": (
                    "Skill arguments — space-separated positional args "
                    "or free-form text depending on the skill"
                ),
            },
        }
        required: list[str] = []

        # 如果 Skill 定义了具体的参数，加入 schema
        if self._skill.arguments:
            for arg in self._skill.arguments:
                properties[arg.name] = {
                    "type": "string",
                    "description": arg.description or arg.name,
                }
                if arg.required:
                    required.append(arg.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def execute(self, **kwargs: Any) -> str:
        """执行 Skill：渲染模板并返回指令文本.

        如果 Skill 有具体参数定义，用 kwargs 中的值替换；
        否则用 arguments 字段作为原始字符串。
        """
        # 构造 arguments 字符串
        try:
            raw_args = _normalize_skill_text_arg(
                kwargs.get("arguments", ""),
                field_name="arguments",
                required=False,
            )
        except ValueError as e:
            return f"Skill 执行已拒绝: {e}"

        # 如果有具体参数定义，将 kwargs 转为额外变量
        extra_vars: dict[str, str] = {}
        if self._skill.arguments:
            for arg in self._skill.arguments:
                if arg.name in kwargs:
                    try:
                        extra_vars[arg.name] = _normalize_skill_text_arg(
                            kwargs[arg.name],
                            field_name=arg.name,
                            required=arg.required,
                        )
                    except ValueError as e:
                        return f"Skill 执行已拒绝: {e}"

            # 拼接位置参数
            parts = []
            for arg in self._skill.arguments:
                val = extra_vars.get(arg.name, arg.default or "")
                if val:
                    parts.append(val)
            if parts:
                raw_args = " ".join(parts)

        rendered = self._skill.render(
            arguments=raw_args,
            extra_vars=extra_vars,
        )
        if len(rendered) > MAX_RENDERED_SKILL_CHARS:
            return (
                "Skill 执行已拒绝: 渲染结果过长，当前上限为 "
                f"{MAX_RENDERED_SKILL_CHARS} 个字符。"
            )

        logger.info(
            "Skill '%s' rendered (%d chars)",
            self._skill.name,
            len(rendered),
        )
        return rendered


class SkillDispatchTool(Tool):
    """统一的 Skill 调度 Tool — Agent 调用 skill_execute(name, arguments).

    这允许用单个 tool 注册所有 skill，而不是为每个 skill 注册一个 tool。
    Engine 持有 SkillLoader 引用来查找具体 skill。
    """

    def __init__(self, skill_names: list[str]) -> None:
        self._skill_names = skill_names

    @property
    def name(self) -> str:
        return "skill_execute"

    @property
    def description(self) -> str:
        names = ", ".join(self._skill_names[:10])
        suffix = "..." if len(self._skill_names) > 10 else ""
        return (
            f"Execute a named skill. Available skills: {names}{suffix}. "
            "Renders the skill's instruction template with given arguments."
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="执行 Skill",
            search_hint="skill execute dispatch render instructions",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to execute",
                    "enum": self._skill_names,
                },
                "arguments": {
                    "type": "string",
                    "description": "Arguments to pass to the skill",
                },
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        # 实际执行由 engine 层处理
        raise NotImplementedError(
            "SkillDispatchTool.execute must be overridden by engine"
        )


def create_skill_tools(skills: list[Skill]) -> list[Tool]:
    """为每个 Skill 创建对应的 SkillTool.

    如果只有少量 skill，为每个创建独立 tool（更精确的 schema）。
    如果 skill 数量很多（>10），创建统一的 dispatch tool。
    """
    if not skills:
        return []

    if len(skills) <= 10:
        return [SkillTool(skill) for skill in skills]

    # 大量 skill 时使用 dispatch tool
    return [SkillDispatchTool([s.name for s in skills])]


def _normalize_skill_text_arg(
    value: Any,
    *,
    field_name: str,
    required: bool,
) -> str:
    """Validate skill arguments before template rendering."""
    if value is None:
        if required:
            raise ValueError(f"{field_name} 不能为空。")
        return ""
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field_name} 不能为空。")
    if len(normalized) > MAX_SKILL_ARGUMENT_CHARS:
        raise ValueError(
            f"{field_name} 过长，当前上限为 "
            f"{MAX_SKILL_ARGUMENT_CHARS} 个字符。"
        )
    return normalized
