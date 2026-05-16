"""Skill 系统 — 可扩展的指令模板，支持用户和 Agent 双通道调用.

每个 Skill 由一个目录表示，核心是 SKILL.md 文件：
  - YAML frontmatter 定义元数据（名称、描述、参数等）
  - Markdown body 定义指令模板（支持变量替换和动态上下文注入）

调用方式：
  1. CLI: /skill-name <args>
  2. LLM Tool: skill_execute(name="skill-name", arguments="...")
"""

from naumi_agent.skills.loader import SkillLoader
from naumi_agent.skills.skill import Skill, SkillArgument, SkillError
from naumi_agent.skills.tool import SkillTool, create_skill_tools

__all__ = [
    "Skill",
    "SkillArgument",
    "SkillError",
    "SkillLoader",
    "SkillTool",
    "create_skill_tools",
]
