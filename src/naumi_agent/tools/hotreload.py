"""热重载 — Agent 运行时重新加载自身模块，无需重启."""

from __future__ import annotations

import importlib
import logging
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

# Modules that must NEVER be reloaded (would break the agent).
_PROTECTED_PREFIXES = (
    "naumi_agent.orchestrator.engine",
    "naumi_agent.orchestrator.subagent_manager",
    "naumi_agent.safety.",
    "naumi_agent.config.",
    "naumi_agent.model.router",
    "naumi_agent.tools.hotreload",
    "naumi_agent.tools.base",
)

_MODULE_NAME_RE = re.compile(r"^[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+$")

# Modules that are safe to reload, grouped by domain.
_RELOADABLE_DOMAINS: dict[str, list[str]] = {
    "memory": [
        "naumi_agent.memory.long_term",
        "naumi_agent.memory.session",
        "naumi_agent.memory.compactor",
    ],
    "skills": [
        "naumi_agent.skills.skill",
        "naumi_agent.skills.loader",
        "naumi_agent.skills.tool",
    ],
    "agents": [
        "naumi_agent.agents.base",
        "naumi_agent.agents.factory",
        "naumi_agent.agents.presets",
        "naumi_agent.agents.message_bus",
    ],
    "hooks": [
        "naumi_agent.hooks.hook_manager",
        "naumi_agent.hooks.shell_hook",
    ],
}

_RELOADABLE_DOMAIN_NAMES = ("tools", *_RELOADABLE_DOMAINS.keys())


def _is_protected(module_name: str) -> bool:
    """Check if a module is in the protected zone."""
    for prefix in _PROTECTED_PREFIXES:
        if module_name == prefix or module_name.startswith(prefix.rstrip(".") + "."):
            return True
    return False


def _normalize_reload_target(target: Any) -> str:
    """Validate public reload targets before resolving modules."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target 不能为空，且必须是字符串。")
    target = target.strip()
    if target == "all" or target in _RELOADABLE_DOMAIN_NAMES:
        return target
    if not target.startswith("naumi_agent."):
        raise ValueError("target 必须是 all、可重载域名，或 naumi_agent.* 模块名。")
    if not _MODULE_NAME_RE.fullmatch(target):
        raise ValueError("target 模块名格式无效。")
    return target


def _discover_tool_modules() -> list[str]:
    """Discover reloadable modules under naumi_agent.tools."""
    tools_dir = Path(__file__).resolve().parent
    modules: list[str] = []
    for py_file in sorted(tools_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        relative = py_file.relative_to(tools_dir).with_suffix("")
        module_name = "naumi_agent.tools." + ".".join(relative.parts)
        if not _MODULE_NAME_RE.fullmatch(module_name):
            continue
        if _is_protected(module_name):
            continue
        modules.append(module_name)
    return modules


def _domain_modules(domain: str) -> list[str]:
    """Resolve a reloadable domain to module names."""
    if domain == "tools":
        return _discover_tool_modules()
    return list(_RELOADABLE_DOMAINS[domain])


def _resolve_modules(target: str) -> list[str]:
    """Resolve a target string to a list of module names.

    Args:
        target: "all", domain name ("tools"), or module path
    """
    target = _normalize_reload_target(target)
    if target == "all":
        modules: list[str] = []
        for domain in _RELOADABLE_DOMAIN_NAMES:
            modules.extend(_domain_modules(domain))
        return modules

    if target in _RELOADABLE_DOMAIN_NAMES:
        return _domain_modules(target)

    # Treat as a module path
    return [target]


def reload_module(module_name: str) -> dict[str, Any]:
    """Reload a single Python module.

    Returns:
        Dict with 'status', 'module', 'error' keys.
    """
    try:
        module_name = _normalize_reload_target(module_name)
    except ValueError as e:
        return {
            "status": "rejected",
            "module": str(module_name),
            "error": str(e),
        }
    if module_name == "all" or module_name in _RELOADABLE_DOMAIN_NAMES:
        return {
            "status": "rejected",
            "module": module_name,
            "error": "reload_module 只接受具体 naumi_agent.* 模块名。",
        }

    if _is_protected(module_name):
        return {
            "status": "protected",
            "module": module_name,
            "error": "模块在保护区内，禁止热重载",
        }

    if module_name not in sys.modules:
        # Module not loaded yet — try importing first
        try:
            importlib.import_module(module_name)
        except Exception as e:
            return {
                "status": "not_found",
                "module": module_name,
                "error": str(e),
            }

    old_module = sys.modules[module_name]
    getattr(old_module, "__file__", "unknown")

    try:
        new_module = importlib.reload(old_module)
        new_version = getattr(new_module, "__file__", "unknown")

        logger.info("Hot-reloaded module: %s", module_name)
        return {
            "status": "reloaded",
            "module": module_name,
            "path": new_version,
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.warning("Hot-reload failed for %s: %s", module_name, e)
        return {
            "status": "error",
            "module": module_name,
            "error": str(e),
            "traceback": tb,
        }


def reload_domain(domain: str) -> list[dict[str, Any]]:
    """Reload all modules in a domain.

    Args:
        domain: "tools", "memory", "skills", "agents", "hooks", or "all"
    """
    try:
        modules = _resolve_modules(domain)
    except ValueError as e:
        return [{"status": "rejected", "module": str(domain), "error": str(e)}]
    results: list[dict[str, Any]] = []

    for mod_name in modules:
        result = reload_module(mod_name)
        results.append(result)

    return results


def list_reloadable() -> dict[str, list[str]]:
    """Return the map of reloadable domains and their modules."""
    return {
        domain: _domain_modules(domain)
        for domain in _RELOADABLE_DOMAIN_NAMES
    }


def get_module_source_path(module_name: str) -> str | None:
    """Get the file path for a module name."""
    mod = sys.modules.get(module_name)
    if mod is None:
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            return None
    return getattr(mod, "__file__", None)


class HotReloadTool(Tool):
    """热重载 — 运行时重新加载 Agent 自身模块."""

    @property
    def name(self) -> str:
        return "hot_reload"

    @property
    def description(self) -> str:
        return (
            "热重载 Agent 自身模块（无需重启）。"
            "支持按域名（tools/memory/skills）或指定模块名重载。"
            "修改源码后调用此工具让改动立即生效。"
            "核心模块（engine/safety/config）受保护，不可重载。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="热重载",
            search_hint="reload hot reload tools memory skills runtime module",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "重载目标: all | tools | memory | skills | agents | hooks"
                        " | 具体模块名 (如 naumi_agent.tools.analysis)"
                    ),
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, **kwargs: Any) -> str:
        results = reload_domain(target)

        parts: list[str] = ["## 热重载结果"]
        reloaded = 0
        errors = 0
        protected = 0

        for r in results:
            status = r["status"]
            mod = r["module"]
            if status == "reloaded":
                parts.append(f"- ✅ {mod}")
                reloaded += 1
            elif status == "protected":
                parts.append(f"- 🔒 {mod} — 受保护，已跳过")
                protected += 1
            elif status == "error":
                parts.append(f"- ❌ {mod} — {r['error']}")
                errors += 1
            elif status == "rejected":
                parts.append(f"- ❌ {mod} — {r['error']}")
                errors += 1
            else:
                parts.append(f"- ⚠️ {mod} — {r['error']}")

        parts.append(
            f"\n**统计**: {reloaded} 重载, {errors} 失败, {protected} 受保护",
        )

        if target == "tools":
            parts.append(
                "\n💡 提示: 工具模块已重载，但注册表仍指向旧实例。"
                "如需生效，请通过 /reload tools 重新注册。",
            )

        return "\n".join(parts)
