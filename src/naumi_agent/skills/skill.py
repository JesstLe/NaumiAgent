"""Skill 数据模型与解析器.

SKILL.md 格式::

    ---
    name: my-skill
    description: 做什么有用的事情
    arguments:
      - name: target
        description: 处理目标
        required: true
    allowed_tools:
      - file_read
      - bash_run
    ---

    # 指令模板

    请对 $ARGUMENTS 执行以下操作...
    动态上下文：`!`echo hello``

解析流程：
  1. 分离 YAML frontmatter 和 markdown body
  2. 解析 frontmatter → SkillArgument 列表 + 元数据
  3. 处理 markdown body 中的动态上下文注入（`!`command``）
  4. 模板渲染时替换 $ARGUMENTS, $0, $1, ${SKILL_DIR} 等
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)
_DYNAMIC_CONTEXT_RE = re.compile(
    r"`!`([^`]+)``",
)
_ARG_SUBST_RE = re.compile(r"\$(\d+)")
_ARGUMENTS_RE = re.compile(r"\$ARGUMENTS\b")
_SKILL_DIR_RE = re.compile(r"\$\{SKILL_DIR\}")


class SkillError(Exception):
    """Skill 相关错误."""


@dataclass(frozen=True)
class SkillArgument:
    """Skill 参数定义."""

    name: str
    description: str = ""
    required: bool = False
    default: str | None = None


@dataclass
class Skill:
    """一个已加载的 Skill.

    directory: SKILL.md 所在目录（用于解析相对路径和支持文件）
    name: 唯一标识符（用于 /name CLI 命令和 skill_execute tool 参数）
    description: 描述（用于 help 文本和 tool description）
    instructions: 指令模板（markdown body，经过 frontmatter 分离后的原始文本）
    arguments: 参数定义列表
    allowed_tools: 限定可用工具列表（空 = 不限制）
    metadata: frontmatter 中的其他字段
    """

    directory: Path
    name: str
    description: str = ""
    instructions: str = ""
    arguments: list[SkillArgument] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable_flag: bool = True
    agent_skill_flag: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def skill_dir(self) -> str:
        """Skill 目录的字符串路径，用于 ${SKILL_DIR} 替换."""
        return str(self.directory)

    @property
    def user_invocable(self) -> bool:
        """是否可通过 CLI 斜杠命令调用."""
        return self.user_invocable_flag

    @property
    def is_agent_skill(self) -> bool:
        """是否以子 agent 模式执行（独立上下文）."""
        return self.agent_skill_flag

    def render(
        self,
        arguments: str = "",
        extra_vars: dict[str, str] | None = None,
        inject_dynamic: bool = True,
    ) -> str:
        """渲染指令模板.

        Args:
            arguments: 用户传入的原始参数字符串
            extra_vars: 额外的变量替换映射
            inject_dynamic: 是否执行动态上下文注入（`!`command``）

        Returns:
            渲染后的完整指令文本
        """
        text = self.instructions

        # 1. 替换 ${SKILL_DIR}
        text = _SKILL_DIR_RE.sub(self.skill_dir, text)

        # 2. 替换 $ARGUMENTS
        text = _ARGUMENTS_RE.sub(arguments, text)

        # 3. 按空格拆分 arguments，替换 $0, $1, ...
        parts = arguments.split() if arguments else []
        text = _ARG_SUBST_RE.sub(
            lambda m: (
                parts[int(m.group(1))] if int(m.group(1)) < len(parts) else ""
            ),
            text,
        )

        # 4. 额外变量替换
        if extra_vars:
            for key, value in extra_vars.items():
                text = text.replace(f"${{{key}}}", value)
                text = text.replace(f"${key}", value)

        # 5. 动态上下文注入
        if inject_dynamic:
            text = self._inject_dynamic_context(text)

        return text

    def _inject_dynamic_context(self, text: str) -> str:
        """执行 `!`command`` 动态上下文注入.

        每个 `!`command`` 会被替换为命令的 stdout 输出。
        """
        def _run_command(match: re.Match) -> str:
            cmd = match.group(1)
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(self.directory),
                )
                if result.returncode == 0:
                    return result.stdout.strip()
                logger.warning(
                    "Dynamic context command failed (%d): %s",
                    result.returncode,
                    cmd,
                )
                return f"[command failed: {cmd}]"
            except subprocess.TimeoutExpired:
                logger.warning("Dynamic context command timed out: %s", cmd)
                return f"[command timed out: {cmd}]"
            except Exception as e:
                logger.warning("Dynamic context command error: %s: %s", cmd, e)
                return f"[command error: {cmd}]"

        return _DYNAMIC_CONTEXT_RE.sub(_run_command, text)


def parse_skill_md(path: Path) -> Skill:
    """解析 SKILL.md 文件为 Skill 对象.

    Args:
        path: SKILL.md 文件路径

    Returns:
        解析后的 Skill 对象

    Raises:
        SkillError: 文件格式错误
    """
    if not path.exists():
        raise SkillError(f"Skill file not found: {path}")

    raw = path.read_text(encoding="utf-8")

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillError(
            f"Invalid SKILL.md format (missing frontmatter): {path}"
        )

    frontmatter_text, body = match.group(1), match.group(2)

    try:
        import yaml

        fm: dict[str, Any] = yaml.safe_load(frontmatter_text) or {}
    except Exception as e:
        raise SkillError(f"Invalid YAML frontmatter in {path}: {e}") from e

    name = fm.get("name")
    if not name:
        raise SkillError(f"Skill missing 'name' in frontmatter: {path}")

    # 安全化 name：只允许小写字母、数字、连字符、下划线
    safe_name = re.sub(r"[^a-z0-9_\-]", "-", str(name).lower())
    if safe_name != name:
        logger.warning(
            "Skill name '%s' normalized to '%s'", name, safe_name,
        )
        name = safe_name

    description = fm.get("description", "")

    # 解析参数
    arguments: list[SkillArgument] = []
    for arg_def in fm.get("arguments", []):
        if isinstance(arg_def, str):
            arguments.append(SkillArgument(name=arg_def))
        elif isinstance(arg_def, dict):
            arguments.append(SkillArgument(
                name=arg_def.get("name", ""),
                description=arg_def.get("description", ""),
                required=arg_def.get("required", False),
                default=arg_def.get("default"),
            ))

    allowed_tools = fm.get("allowed_tools", [])

    # 提取已知的 frontmatter 字段，其余存入 metadata
    known_keys = {
        "name", "description", "arguments", "allowed_tools",
        "when_to_use", "user_invocable", "context",
    }
    metadata = {k: v for k, v in fm.items() if k not in known_keys}

    return Skill(
        directory=path.parent,
        name=str(name),
        description=str(description),
        instructions=body.strip(),
        arguments=arguments,
        allowed_tools=allowed_tools if isinstance(allowed_tools, list) else [],
        user_invocable_flag=fm.get("user_invocable", True),
        agent_skill_flag=fm.get("context") == "fork",
        metadata=metadata,
    )
