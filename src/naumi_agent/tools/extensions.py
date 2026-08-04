"""Read-only extension discovery surfaces shared by users and the agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from naumi_agent.skills.loader import SkillDiscoverySnapshot
from naumi_agent.tools.base import Tool, ToolMetadata


def _markdown_code(value: str | Path) -> str:
    normalized = (
        str(value)
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    fence = "`"
    while fence in normalized:
        fence += "`"
    padding = " " if normalized.startswith("`") or normalized.endswith("`") else ""
    return f"{fence}{padding}{normalized}{padding}{fence}"


def render_skill_discovery(snapshot: SkillDiscoverySnapshot) -> str:
    """Render deterministic source priority and collision decisions."""

    lines = [
        "## 扩展发现 · Skills",
        "",
        (
            f"已选中 **{snapshot.selected_count}** · "
            f"同名遮蔽 **{snapshot.shadowed_count}** · "
            f"无效 **{snapshot.invalid_count}**"
        ),
        "",
        "### 来源优先级",
        "",
    ]
    if not snapshot.sources:
        lines.append("当前没有声明 Skill 来源。")
    for source in snapshot.sources:
        availability = "可用" if source.available else "目录不存在"
        trust = " · 尚待接入 workspace 信任门" if source.requires_trust_gate else ""
        lines.append(
            f"{source.priority + 1}. **{source.scope}** · {availability}{trust} · "
            f"{_markdown_code(source.path)}"
        )

    selected = [item for item in snapshot.candidates if item.state == "selected"]
    lines.extend(["", "### 生效项", ""])
    if not selected:
        lines.append("没有生效的 Skill。")
    else:
        for item in selected:
            lines.append(
                f"- **{item.name}** · {item.source_scope} · "
                f"{_markdown_code(item.manifest_path)}"
            )

    exceptions = [item for item in snapshot.candidates if item.state != "selected"]
    if exceptions:
        lines.extend(["", "### 冲突与错误", ""])
        for item in exceptions:
            if item.state == "shadowed":
                lines.append(
                    f"- **{item.name}** · 已被更高优先级项遮蔽 · "
                    f"候选 {_markdown_code(item.manifest_path)} · "
                    f"生效 {_markdown_code(item.selected_manifest_path or '')}"
                )
            else:
                reason = {
                    "invalid_manifest": "SKILL.md 格式无效",
                    "read_failed": "SKILL.md 读取失败",
                }.get(item.reason_code, "发现失败")
                lines.append(
                    f"- **{item.name}** · {reason} · "
                    f"{_markdown_code(item.manifest_path)}"
                )

    lines.extend(
        [
            "",
            "> 发现只读取清单，不执行动态命令，不安装、启用或授予权限。",
            "> 同名项严格按上方顺序选择，绝不静默覆盖。",
        ]
    )
    return "\n".join(lines)


async def execute_extension_discovery(engine: Any, *, kind: str = "skills") -> str:
    """Execute the shared read-only discovery operation."""

    normalized = str(kind or "skills").strip().lower()
    if normalized not in {"skills", "skill"}:
        raise ValueError("当前仅支持 `/extensions skills`；Plugin 与 MCP 将分阶段接入。")
    loader = getattr(engine, "skill_loader", None)
    snapshot = getattr(loader, "discovery_snapshot", None)
    if not isinstance(snapshot, SkillDiscoverySnapshot):
        raise RuntimeError("Skill 发现权威尚未初始化，请重启 NaumiAgent 后重试。")
    return render_skill_discovery(snapshot)


class ExtensionDiscoveryTool(Tool):
    """Expose the exact Skill discovery result to agent reasoning."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "extension_discovery"

    @property
    def description(self) -> str:
        return (
            "读取实际 Skill 加载器的来源、优先级、生效项、同名遮蔽与无效清单；"
            "不会执行 Skill、联网、安装或改变信任状态。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="扩展来源发现",
            search_hint=(
                "extension skill discovery source priority conflict shadowed manifest"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["skills"],
                    "default": "skills",
                    "description": "当前可检查的扩展类型。",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, *, kind: str = "skills", **kwargs: Any) -> str:
        return await execute_extension_discovery(self._engine, kind=kind)


__all__ = [
    "ExtensionDiscoveryTool",
    "execute_extension_discovery",
    "render_skill_discovery",
]
