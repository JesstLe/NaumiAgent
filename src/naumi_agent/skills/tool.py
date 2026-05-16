"""Skill → Tool 桥接.

将已加载的 Skill 注册为 Agent 可调用的 Tool，
使 LLM 可以在推理链中自主决策何时使用某个 Skill。
"""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.skills.skill import Skill
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


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
        raw_args = kwargs.get("arguments", "")

        # 如果有具体参数定义，将 kwargs 转为额外变量
        extra_vars: dict[str, str] = {}
        if self._skill.arguments:
            for arg in self._skill.arguments:
                if arg.name in kwargs:
                    extra_vars[arg.name] = str(kwargs[arg.name])

            # 拼接位置参数
            parts = []
            for arg in self._skill.arguments:
                val = kwargs.get(arg.name, arg.default or "")
                if val:
                    parts.append(str(val))
            if parts:
                raw_args = " ".join(parts)

        rendered = self._skill.render(
            arguments=raw_args,
            extra_vars=extra_vars,
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
