"""Shared UI theme and output-style policy for CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from prompt_toolkit.styles import Style


class ThemeConfigError(ValueError):
    """Raised when UI theme config is invalid."""


class ThemeName(StrEnum):
    DARK = "dark"
    MINIMAL = "minimal"
    HIGH_CONTRAST = "high_contrast"


class OutputStyleName(StrEnum):
    COMPACT = "compact"
    DETAILED = "detailed"
    DEBUG = "debug"
    SILENT_TOOLS = "silent_tools"


@dataclass(frozen=True)
class ThemePalette:
    name: ThemeName
    label: str
    ansi: dict[str, str]
    prompt_toolkit: dict[str, str]
    tui: dict[str, str]


@dataclass(frozen=True)
class OutputStylePolicy:
    name: OutputStyleName
    label: str
    diff_max_files: int
    show_diff_preview: bool
    show_debug_metadata: bool
    show_tool_details: bool


@dataclass(frozen=True)
class UIStyleConfig:
    theme: ThemePalette
    output_style: OutputStylePolicy

    def ansi(self, token: str, text: str) -> str:
        code = self.theme.ansi.get(token, self.theme.ansi["text"])
        return f"\033[{code}m{text}\033[0m"

    def prompt_toolkit_style(self) -> Style:
        return Style.from_dict(self.theme.prompt_toolkit)

    def tui_css(self) -> str:
        values = self.theme.tui
        return f"""
    Screen {{
        background: {values["surface"]};
        color: {values["text"]};
    }}

    StatusBar {{
        color: {values["status"]};
    }}

    TodoBar {{
        background: {values["surface_alt"]};
        color: {values["warning"]};
    }}

    Spinner {{
        color: {values["success"]};
    }}

    .user-msg {{
        border-left: thick {values["info"]};
    }}

    .agent-msg {{
        border-left: thick {values["success"]};
    }}

    Markdown.user-msg {{
        border-left: thick {values["info"]};
    }}

    Markdown.agent-msg {{
        border-left: thick {values["success"]};
    }}

    .thinking-block {{
        border-left: thick {values["warning"]};
    }}

    .tool-output {{
        border: round {values["accent"]};
    }}
"""


THEMES: dict[ThemeName, ThemePalette] = {
    ThemeName.DARK: ThemePalette(
        name=ThemeName.DARK,
        label="深色",
        ansi={
            "text": "37",
            "muted": "2",
            "success": "32",
            "danger": "31",
            "warning": "33",
            "info": "36",
            "accent": "35",
            "title": "1",
            "diff_add": "32",
            "diff_delete": "31",
            "diff_hunk": "36",
            "permission": "33",
        },
        prompt_toolkit={
            "border": "#444444",
            "border-active": "#00aa00",
            "prompt": "#00aa00 bold",
            "processing": "#888888",
            "status": "#888888",
        },
        tui={
            "surface": "#121212",
            "surface_alt": "#1e1e1e",
            "text": "#e6e6e6",
            "status": "#9a9a9a",
            "success": "green",
            "danger": "red",
            "warning": "yellow",
            "info": "blue",
            "accent": "cyan",
        },
    ),
    ThemeName.MINIMAL: ThemePalette(
        name=ThemeName.MINIMAL,
        label="极简",
        ansi={
            "text": "37",
            "muted": "2",
            "success": "37",
            "danger": "37",
            "warning": "37",
            "info": "37",
            "accent": "37",
            "title": "1",
            "diff_add": "37",
            "diff_delete": "37",
            "diff_hunk": "2",
            "permission": "37",
        },
        prompt_toolkit={
            "border": "#666666",
            "border-active": "#ffffff",
            "prompt": "#ffffff bold",
            "processing": "#777777",
            "status": "#777777",
        },
        tui={
            "surface": "#101010",
            "surface_alt": "#181818",
            "text": "#eeeeee",
            "status": "#aaaaaa",
            "success": "white",
            "danger": "white",
            "warning": "white",
            "info": "white",
            "accent": "white",
        },
    ),
    ThemeName.HIGH_CONTRAST: ThemePalette(
        name=ThemeName.HIGH_CONTRAST,
        label="高对比",
        ansi={
            "text": "97",
            "muted": "97",
            "success": "92;1",
            "danger": "91;1",
            "warning": "93;1",
            "info": "96;1",
            "accent": "95;1",
            "title": "97;1",
            "diff_add": "92;1",
            "diff_delete": "91;1",
            "diff_hunk": "96;1",
            "permission": "93;1",
        },
        prompt_toolkit={
            "border": "#ffffff",
            "border-active": "#00ff00 bold",
            "prompt": "#00ff00 bold",
            "processing": "#ffff00 bold",
            "status": "#ffffff",
        },
        tui={
            "surface": "#000000",
            "surface_alt": "#000000",
            "text": "#ffffff",
            "status": "#ffffff",
            "success": "#00ff00",
            "danger": "#ff4040",
            "warning": "#ffff00",
            "info": "#00ffff",
            "accent": "#ff00ff",
        },
    ),
}

OUTPUT_STYLES: dict[OutputStyleName, OutputStylePolicy] = {
    OutputStyleName.COMPACT: OutputStylePolicy(
        name=OutputStyleName.COMPACT,
        label="紧凑",
        diff_max_files=8,
        show_diff_preview=True,
        show_debug_metadata=False,
        show_tool_details=True,
    ),
    OutputStyleName.DETAILED: OutputStylePolicy(
        name=OutputStyleName.DETAILED,
        label="详细",
        diff_max_files=20,
        show_diff_preview=True,
        show_debug_metadata=False,
        show_tool_details=True,
    ),
    OutputStyleName.DEBUG: OutputStylePolicy(
        name=OutputStyleName.DEBUG,
        label="调试",
        diff_max_files=50,
        show_diff_preview=True,
        show_debug_metadata=True,
        show_tool_details=True,
    ),
    OutputStyleName.SILENT_TOOLS: OutputStylePolicy(
        name=OutputStyleName.SILENT_TOOLS,
        label="静默工具",
        diff_max_files=12,
        show_diff_preview=False,
        show_debug_metadata=False,
        show_tool_details=False,
    ),
}


def build_ui_style_config(
    theme: str | ThemeName = ThemeName.DARK,
    output_style: str | OutputStyleName = OutputStyleName.DETAILED,
) -> UIStyleConfig:
    """Resolve user config into a validated style config."""
    theme_name = _coerce_theme(theme)
    style_name = _coerce_output_style(output_style)
    return UIStyleConfig(theme=THEMES[theme_name], output_style=OUTPUT_STYLES[style_name])


def build_ui_style_from_config(config: Any | None) -> UIStyleConfig:
    """Read style config from AppConfig-like objects."""
    ui_config = getattr(config, "ui", None)
    theme = getattr(ui_config, "theme", ThemeName.DARK)
    output_style = getattr(ui_config, "output_style", OutputStyleName.DETAILED)
    return build_ui_style_config(theme=theme, output_style=output_style)


def render_style_help(style: UIStyleConfig) -> str:
    lines = [
        "## 界面样式",
        "",
        f"- 主题：`{style.theme.name.value}`（{style.theme.label}）",
        f"- 输出风格：`{style.output_style.name.value}`（{style.output_style.label}）",
        "",
        "### 可用主题",
    ]
    for theme in THEMES.values():
        lines.append(f"- `{theme.name.value}` — {theme.label}")
    lines.append("")
    lines.append("### 可用输出风格")
    for policy in OUTPUT_STYLES.values():
        lines.append(f"- `{policy.name.value}` — {policy.label}")
    lines.append("")
    lines.append("配置方式：在 `config.yaml` 中设置 `ui.theme` 和 `ui.output_style`。")
    return "\n".join(lines)


def _coerce_theme(value: str | ThemeName) -> ThemeName:
    normalized = str(value).strip().lower().replace("-", "_")
    try:
        return ThemeName(normalized)
    except ValueError as exc:
        available = "、".join(theme.value for theme in ThemeName)
        raise ThemeConfigError(f"未知主题 `{value}`。可用主题：{available}") from exc


def _coerce_output_style(value: str | OutputStyleName) -> OutputStyleName:
    normalized = str(value).strip().lower().replace("-", "_")
    try:
        return OutputStyleName(normalized)
    except ValueError as exc:
        available = "、".join(style.value for style in OutputStyleName)
        raise ThemeConfigError(f"未知输出风格 `{value}`。可用风格：{available}") from exc
