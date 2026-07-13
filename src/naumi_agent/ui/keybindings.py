"""Shared keybinding configuration for CLI and TUI surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

InterfaceName = Literal["cli", "tui"]
ScopeName = Literal["normal", "permission"]


class KeybindingConfigError(ValueError):
    """Raised when a user keybinding override is invalid."""


class KeybindingAction(StrEnum):
    """Stable action ids accepted by config.yaml keybinding overrides."""

    MODE_CYCLE = "mode_cycle"
    PERMISSION_ALLOW = "permission_allow"
    PERMISSION_DENY = "permission_deny"
    PERMISSION_BYPASS = "permission_bypass"
    SUBMIT = "submit"
    INTERRUPT = "interrupt"
    EXIT = "exit"
    SCROLL_PAGE_UP = "scroll_page_up"
    SCROLL_PAGE_DOWN = "scroll_page_down"
    COPY_TRANSCRIPT = "copy_transcript"
    TUI_QUIT = "tui_quit"
    TOGGLE_ACTIVITY = "toggle_activity"
    TOGGLE_INSPECTOR = "toggle_inspector"
    OPEN_AGENTS = "open_agents"
    TOGGLE_HISTORY = "toggle_history"
    CLEAR_CHAT = "clear_chat"
    SHOW_TOOLS = "show_tools"
    TOGGLE_BROWSER = "toggle_browser"


@dataclass(frozen=True)
class KeybindingDefinition:
    action: KeybindingAction
    description: str
    default_keys: tuple[str, ...]
    scope: ScopeName = "normal"
    interfaces: tuple[InterfaceName, ...] = ("cli", "tui")
    textual_action: str | None = None
    textual_priority: bool = False


@dataclass(frozen=True)
class Keybinding:
    definition: KeybindingDefinition
    keys: tuple[str, ...]

    @property
    def action(self) -> KeybindingAction:
        return self.definition.action

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def scope(self) -> ScopeName:
        return self.definition.scope

    @property
    def interfaces(self) -> tuple[InterfaceName, ...]:
        return self.definition.interfaces

    @property
    def display_key_list(self) -> str:
        return " / ".join(display_key(key) for key in self.keys)


class KeybindingSet:
    """Validated keybindings resolved from defaults plus user overrides."""

    def __init__(self, bindings: dict[KeybindingAction, Keybinding]) -> None:
        self._bindings = bindings

    def get(self, action: KeybindingAction | str) -> Keybinding:
        return self._bindings[KeybindingAction(action)]

    def keys_for(
        self,
        action: KeybindingAction | str,
        *,
        interface: InterfaceName,
    ) -> tuple[str, ...]:
        binding = self.get(action)
        if interface not in binding.interfaces:
            return ()
        return binding.keys

    def display_keys_for(
        self,
        action: KeybindingAction | str,
        *,
        interface: InterfaceName,
    ) -> str:
        keys = self.keys_for(action, interface=interface)
        return " / ".join(display_key(key) for key in keys)

    def rows_for(self, *, interface: InterfaceName) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for definition in KEYBINDING_DEFINITIONS:
            binding = self._bindings[definition.action]
            if interface not in binding.interfaces:
                continue
            rows.append(
                (
                    binding.display_key_list,
                    binding.description,
                    _scope_label(binding.scope),
                )
            )
        return rows

    def as_config_dict(self) -> dict[str, list[str]]:
        return {
            action.value: list(binding.keys)
            for action, binding in self._bindings.items()
        }


KEYBINDING_DEFINITIONS: tuple[KeybindingDefinition, ...] = (
    KeybindingDefinition(
        KeybindingAction.MODE_CYCLE,
        "切换运行模式",
        ("s-tab",),
        textual_action="cycle_runtime_mode",
        textual_priority=True,
    ),
    KeybindingDefinition(
        KeybindingAction.PERMISSION_ALLOW,
        "权限确认：允许一次",
        ("y",),
        scope="permission",
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.PERMISSION_DENY,
        "权限确认：拒绝",
        ("n",),
        scope="permission",
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.PERMISSION_BYPASS,
        "权限确认：切换 bypass 并执行",
        ("s-tab",),
        scope="permission",
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.SUBMIT,
        "提交输入",
        ("enter",),
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.INTERRUPT,
        "连续中断当前输出",
        ("escape",),
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.EXIT,
        "退出界面",
        ("c-c", "c-d"),
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.SCROLL_PAGE_UP,
        "向上翻页",
        ("pageup",),
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.SCROLL_PAGE_DOWN,
        "向下翻页",
        ("pagedown",),
        interfaces=("cli",),
    ),
    KeybindingDefinition(
        KeybindingAction.COPY_TRANSCRIPT,
        "复制/导出记录",
        ("c-y",),
        textual_action="copy_transcript",
    ),
    KeybindingDefinition(
        KeybindingAction.TUI_QUIT,
        "退出界面",
        ("c-q",),
        interfaces=("tui",),
        textual_action="quit",
    ),
    KeybindingDefinition(
        KeybindingAction.TOGGLE_ACTIVITY,
        "显示/隐藏活动面板",
        ("tab",),
        interfaces=("tui",),
        textual_action="toggle_activity",
    ),
    KeybindingDefinition(
        KeybindingAction.TOGGLE_INSPECTOR,
        "打开/关闭 Runtime Inspector",
        ("c-i",),
        interfaces=("tui",),
        textual_action="toggle_inspector",
        textual_priority=True,
    ),
    KeybindingDefinition(
        KeybindingAction.OPEN_AGENTS,
        "打开/关闭 Agent 控制中心",
        ("c-g",),
        interfaces=("tui",),
        textual_action="toggle_agents",
        textual_priority=True,
    ),
    KeybindingDefinition(
        KeybindingAction.TOGGLE_HISTORY,
        "显示/隐藏历史面板",
        ("c-h",),
        interfaces=("tui",),
        textual_action="toggle_history",
    ),
    KeybindingDefinition(
        KeybindingAction.CLEAR_CHAT,
        "清空当前会话",
        ("c-l",),
        interfaces=("tui",),
        textual_action="clear_chat",
    ),
    KeybindingDefinition(
        KeybindingAction.SHOW_TOOLS,
        "显示工具列表",
        ("c-t",),
        interfaces=("tui",),
        textual_action="show_tools",
    ),
    KeybindingDefinition(
        KeybindingAction.TOGGLE_BROWSER,
        "显示/隐藏浏览器面板",
        ("c-b",),
        interfaces=("tui",),
        textual_action="toggle_browser",
    ),
)

_ACTION_NAMES = {definition.action.value for definition in KEYBINDING_DEFINITIONS}

_KEY_ALIASES = {
    "ctrl": "c",
    "control": "c",
    "esc": "escape",
    "return": "enter",
    "page-up": "pageup",
    "pgup": "pageup",
    "page-down": "pagedown",
    "pgdn": "pagedown",
    "shift-tab": "s-tab",
}


def build_keybindings(overrides: dict[str, Any] | None = None) -> KeybindingSet:
    """Merge config overrides with defaults and validate key conflicts."""
    normalized_overrides = _normalize_overrides(overrides or {})
    bindings: dict[KeybindingAction, Keybinding] = {}
    for definition in KEYBINDING_DEFINITIONS:
        keys = normalized_overrides.get(definition.action, definition.default_keys)
        if not keys:
            raise KeybindingConfigError(
                f"快捷键动作 `{definition.action.value}` 至少需要一个按键。"
            )
        bindings[definition.action] = Keybinding(definition=definition, keys=keys)
    _validate_conflicts(bindings)
    return KeybindingSet(bindings)


def normalize_key_name(raw_key: str) -> str:
    """Normalize config-friendly key names to prompt_toolkit-style names."""
    key = raw_key.strip().lower().replace(" ", "")
    if not key:
        raise KeybindingConfigError("快捷键不能为空。")
    key = key.replace("_", "-")
    if key in _KEY_ALIASES:
        return _KEY_ALIASES[key]
    if "+" in key:
        parts = [part for part in key.split("+") if part]
        if not parts:
            raise KeybindingConfigError(f"快捷键 `{raw_key}` 无法解析。")
        mods = [_KEY_ALIASES.get(part, part) for part in parts[:-1]]
        base = _KEY_ALIASES.get(parts[-1], parts[-1])
        if mods == ["shift"] and base == "tab":
            return "s-tab"
        if mods == ["c"] and len(base) == 1:
            return f"c-{base}"
        if mods == ["s"] and base == "tab":
            return "s-tab"
        raise KeybindingConfigError(
            f"暂不支持快捷键 `{raw_key}`；请使用 y、enter、pageup、Ctrl+Y、Shift+Tab 等格式。"
        )
    return key


def display_key(key: str) -> str:
    if key.startswith("c-") and len(key) == 3:
        return f"Ctrl+{key[-1].upper()}"
    if key == "s-tab":
        return "Shift+Tab"
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    labels = {
        "enter": "Enter",
        "escape": "Esc",
        "pageup": "PageUp",
        "pagedown": "PageDown",
        "tab": "Tab",
    }
    return labels.get(key, key.upper() if len(key) == 1 else key)


def to_textual_key(key: str) -> str:
    """Convert normalized keys to Textual binding key names."""
    if key.startswith("c-") and len(key) == 3:
        return f"ctrl+{key[-1]}"
    if key == "s-tab":
        return "shift+tab"
    return key


def render_keybinding_help(
    keybindings: KeybindingSet | None = None,
    *,
    interface: InterfaceName,
) -> str:
    """Render a Chinese Markdown help panel for configured shortcuts."""
    resolved = keybindings or build_keybindings()
    title = "CLI 快捷键" if interface == "cli" else "TUI 快捷键"
    lines = [f"## {title}", ""]
    rows = resolved.rows_for(interface=interface)
    for scope in ("常规", "权限确认"):
        scope_rows = [row for row in rows if row[2] == scope]
        if not scope_rows:
            continue
        lines.append(f"### {scope}")
        for keys, description, _scope in scope_rows:
            lines.append(f"- `{keys}` — {description}")
    lines.append("")
    lines.append("配置方式：在 `config.yaml` 顶层添加 `keybindings:`，用动作名覆盖默认按键。")
    return "\n".join(lines)


def _normalize_overrides(overrides: dict[str, Any]) -> dict[KeybindingAction, tuple[str, ...]]:
    normalized: dict[KeybindingAction, tuple[str, ...]] = {}
    for raw_action, raw_keys in overrides.items():
        try:
            action = KeybindingAction(str(raw_action))
        except ValueError as exc:
            actions = "、".join(sorted(_ACTION_NAMES))
            raise KeybindingConfigError(
                f"未知快捷键动作 `{raw_action}`。可用动作：{actions}"
            ) from exc
        keys = _coerce_keys(raw_keys, action.value)
        seen: set[str] = set()
        deduped: list[str] = []
        for key in keys:
            normalized_key = normalize_key_name(key)
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            deduped.append(normalized_key)
        normalized[action] = tuple(deduped)
    return normalized


def _coerce_keys(raw_keys: Any, action: str) -> list[str]:
    if isinstance(raw_keys, str):
        return [raw_keys]
    if isinstance(raw_keys, list | tuple):
        if not all(isinstance(item, str) for item in raw_keys):
            raise KeybindingConfigError(f"快捷键动作 `{action}` 的每个按键都必须是字符串。")
        return list(raw_keys)
    raise KeybindingConfigError(
        f"快捷键动作 `{action}` 必须配置为字符串或字符串列表。"
    )


def _validate_conflicts(bindings: dict[KeybindingAction, Keybinding]) -> None:
    owners: dict[tuple[InterfaceName, ScopeName, str], KeybindingAction] = {}
    for action, binding in bindings.items():
        for interface in binding.interfaces:
            for key in binding.keys:
                owner_key = (interface, binding.scope, key)
                existing = owners.get(owner_key)
                if existing is not None:
                    raise KeybindingConfigError(
                        "快捷键冲突："
                        f"`{display_key(key)}` 同时绑定到 `{existing.value}` 和 `{action.value}`。"
                    )
                owners[owner_key] = action


def _scope_label(scope: ScopeName) -> str:
    return "权限确认" if scope == "permission" else "常规"
