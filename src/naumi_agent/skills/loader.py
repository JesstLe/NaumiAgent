"""Skill 发现与加载器.

从配置的目录列表中扫描 SKILL.md 文件，解析并注册为可用 Skill。

目录结构约定::

    skills/                     # 搜索路径之一
    ├── code-review/            # 一个 skill
    │   ├── SKILL.md            # 核心定义文件
    │   ├── template.py         # 可选支持文件
    │   └── examples/           # 可选示例
    └── deploy-check/
        ├── SKILL.md
        └── checklist.yaml

搜索路径优先级（高→低）：
  1. 项目目录 .naumi/skills/
  2. 用户目录 ~/.naumi/skills/
  3. 配置文件中指定的额外目录
"""

from __future__ import annotations

import logging
from pathlib import Path

from naumi_agent.skills.skill import Skill, SkillError

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"


class SkillLoader:
    """从文件系统发现并加载 Skill.

    用法::

        loader = SkillLoader(
            search_paths=["skills/", "~/.naumi/skills/"]
        )
        skills = loader.load_all()
        skill = loader.get("code-review")
    """

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._search_paths = self._resolve_paths(search_paths or [])
        self._skills: dict[str, Skill] = {}

    @staticmethod
    def _resolve_paths(paths: list[str]) -> list[Path]:
        """展开 ~ 和相对路径为绝对路径，过滤不存在的目录."""
        resolved: list[Path] = []
        for p in paths:
            path = Path(p).expanduser().resolve()
            if path.is_dir():
                resolved.append(path)
            elif path.exists():
                logger.warning("Skill path is not a directory: %s", path)
        return resolved

    def load_all(self) -> list[Skill]:
        """扫描所有搜索路径，加载发现的 Skill.

        如果多个路径包含同名 Skill，先发现的优先（高优先级路径应排在前面）。
        返回所有成功加载的 Skill 列表。
        """
        loaded: list[Skill] = []
        seen_names: set[str] = set()

        for search_dir in self._search_paths:
            if not search_dir.is_dir():
                continue

            for skill_dir in sorted(search_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue

                skill_file = skill_dir / _SKILL_FILE
                if not skill_file.is_file():
                    continue

                try:
                    skill = self._load_one(skill_file)
                except SkillError as e:
                    logger.warning("Failed to load skill: %s", e)
                    continue
                except Exception:
                    logger.exception(
                        "Unexpected error loading skill: %s", skill_file,
                    )
                    continue

                if skill.name in seen_names:
                    logger.debug(
                        "Skill '%s' already loaded, skipping duplicate: %s",
                        skill.name,
                        skill_file,
                    )
                    continue

                seen_names.add(skill.name)
                self._skills[skill.name] = skill
                loaded.append(skill)
                logger.info("Loaded skill '%s' from %s", skill.name, skill_dir)

        return loaded

    def _load_one(self, path: Path) -> Skill:
        """加载单个 SKILL.md 文件."""
        from naumi_agent.skills.skill import parse_skill_md

        skill = parse_skill_md(path)
        return skill

    def get(self, name: str) -> Skill | None:
        """按名称获取已加载的 Skill."""
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        """返回所有已加载的 Skill."""
        return list(self._skills.values())

    @property
    def names(self) -> list[str]:
        """返回所有已加载的 Skill 名称."""
        return list(self._skills.keys())

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills
