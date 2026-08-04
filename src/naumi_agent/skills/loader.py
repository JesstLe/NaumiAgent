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
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.skills.skill import Skill, SkillError

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class SkillSource:
    """One ordered skill discovery root used by the real loader."""

    scope: str
    path: Path
    priority: int
    available: bool = False
    requires_trust_gate: bool = False


@dataclass(frozen=True)
class SkillCandidate:
    """A selected, shadowed, or invalid SKILL.md discovered on disk."""

    name: str
    manifest_path: Path
    source_scope: str
    source_priority: int
    state: str
    reason_code: str = ""
    selected_manifest_path: Path | None = None


@dataclass(frozen=True)
class SkillDiscoverySnapshot:
    """Exact discovery facts produced while loading skills."""

    sources: tuple[SkillSource, ...]
    candidates: tuple[SkillCandidate, ...]

    @property
    def selected_count(self) -> int:
        return sum(item.state == "selected" for item in self.candidates)

    @property
    def shadowed_count(self) -> int:
        return sum(item.state == "shadowed" for item in self.candidates)

    @property
    def invalid_count(self) -> int:
        return sum(item.state == "invalid" for item in self.candidates)


def build_skill_sources(
    *,
    workspace_root: Path,
    configured_paths: list[str],
    home: Path | None = None,
) -> tuple[SkillSource, ...]:
    """Build and de-duplicate the actual workspace/user/configured precedence."""

    workspace = Path(workspace_root).expanduser().resolve()
    user_home = (home or Path.home()).expanduser().resolve()
    declarations: list[tuple[str, Path, bool]] = [
        ("workspace", workspace / ".naumi" / "skills", True),
        ("user", user_home / ".naumi" / "skills", False),
    ]
    for raw in configured_paths:
        configured = Path(raw).expanduser()
        if not configured.is_absolute():
            configured = workspace / configured
        declarations.append(("configured", configured, False))

    sources: list[SkillSource] = []
    seen: set[Path] = set()
    for _declared_priority, (scope, raw_path, untrusted) in enumerate(declarations):
        path = raw_path.resolve()
        if path in seen:
            continue
        seen.add(path)
        sources.append(
            SkillSource(
                scope=scope,
                path=path,
                priority=len(sources),
                available=path.is_dir(),
                requires_trust_gate=untrusted,
            )
        )
    return tuple(sources)


class SkillLoader:
    """从文件系统发现并加载 Skill.

    用法::

        loader = SkillLoader(
            search_paths=["skills/", "~/.naumi/skills/"]
        )
        skills = loader.load_all()
        skill = loader.get("code-review")
    """

    def __init__(
        self,
        search_paths: list[str] | None = None,
        *,
        sources: tuple[SkillSource, ...] | None = None,
    ) -> None:
        if sources is None:
            resolved = self._resolve_paths(search_paths or [])
            self._sources = tuple(
                SkillSource(
                    scope="configured",
                    path=path,
                    priority=index,
                    available=True,
                )
                for index, path in enumerate(resolved)
            )
        else:
            self._sources = tuple(sources)
        self._skills: dict[str, Skill] = {}
        self._discovery_snapshot = SkillDiscoverySnapshot(
            sources=self._sources,
            candidates=(),
        )

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
        seen_names: dict[str, Path] = {}
        candidates: list[SkillCandidate] = []
        self._skills.clear()

        self._sources = tuple(
            SkillSource(
                scope=source.scope,
                path=source.path,
                priority=source.priority,
                available=source.path.is_dir(),
                requires_trust_gate=source.requires_trust_gate,
            )
            for source in self._sources
        )

        for source in self._sources:
            search_dir = source.path
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
                    candidates.append(
                        SkillCandidate(
                            name=skill_dir.name,
                            manifest_path=skill_file,
                            source_scope=source.scope,
                            source_priority=source.priority,
                            state="invalid",
                            reason_code="invalid_manifest",
                        )
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Unexpected error loading skill: %s", skill_file,
                    )
                    candidates.append(
                        SkillCandidate(
                            name=skill_dir.name,
                            manifest_path=skill_file,
                            source_scope=source.scope,
                            source_priority=source.priority,
                            state="invalid",
                            reason_code="read_failed",
                        )
                    )
                    continue

                if skill.name in seen_names:
                    logger.debug(
                        "Skill '%s' already loaded, skipping duplicate: %s",
                        skill.name,
                        skill_file,
                    )
                    candidates.append(
                        SkillCandidate(
                            name=skill.name,
                            manifest_path=skill_file,
                            source_scope=source.scope,
                            source_priority=source.priority,
                            state="shadowed",
                            reason_code="lower_priority_duplicate",
                            selected_manifest_path=seen_names[skill.name],
                        )
                    )
                    continue

                seen_names[skill.name] = skill_file
                self._skills[skill.name] = skill
                loaded.append(skill)
                candidates.append(
                    SkillCandidate(
                        name=skill.name,
                        manifest_path=skill_file,
                        source_scope=source.scope,
                        source_priority=source.priority,
                        state="selected",
                    )
                )
                logger.info("Loaded skill '%s' from %s", skill.name, skill_dir)

        self._discovery_snapshot = SkillDiscoverySnapshot(
            sources=self._sources,
            candidates=tuple(candidates),
        )
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
    def discovery_snapshot(self) -> SkillDiscoverySnapshot:
        """Return the same ordered discovery facts that selected loaded skills."""

        return self._discovery_snapshot

    @property
    def names(self) -> list[str]:
        """返回所有已加载的 Skill 名称."""
        return list(self._skills.keys())

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills
