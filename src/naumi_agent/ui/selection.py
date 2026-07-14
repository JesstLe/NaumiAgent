"""Cross-platform keyboard choice selection for terminal entry flows."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from prompt_toolkit.shortcuts import choice


@dataclass(frozen=True, slots=True)
class TerminalChoice:
    """One stable terminal choice with user-visible text."""

    value: str
    label: str
    description: str = ""

    @property
    def display_label(self) -> str:
        if not self.description:
            return self.label
        return f"{self.label} · {self.description}"


ChoiceFallback = Callable[[tuple[TerminalChoice, ...], str], str]


def select_terminal_choice(
    message: str,
    options: Sequence[TerminalChoice],
    *,
    default: str,
    interactive: bool | None = None,
    fallback: ChoiceFallback | None = None,
) -> str:
    """Select one option using arrows in a TTY and a caller fallback elsewhere."""
    normalized = tuple(options)
    _validate_choices(normalized, default)
    use_interactive = _is_interactive_terminal() if interactive is None else interactive

    if use_interactive:
        try:
            return choice(
                message=message,
                options=[(option.value, option.display_label) for option in normalized],
                default=default,
                symbol="›",
                bottom_toolbar="↑/↓ 选择 · 数字定位 · Enter 确认 · Ctrl+C 取消",
            )
        except EOFError:
            if fallback is None:
                raise

    selected = (fallback or _plain_fallback)(normalized, default)
    valid_values = {option.value for option in normalized}
    if selected not in valid_values:
        raise ValueError(f"选择结果不在可用选项中: {selected}")
    return selected


def _validate_choices(options: tuple[TerminalChoice, ...], default: str) -> None:
    if not options:
        raise ValueError("至少提供一个选项")
    values = [option.value for option in options]
    if any(not value for value in values):
        raise ValueError("选项值不能为空")
    if len(values) != len(set(values)):
        raise ValueError("选项值不能重复")
    if default not in values:
        raise ValueError("默认值必须属于选项")


def _is_interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _plain_fallback(options: tuple[TerminalChoice, ...], default: str) -> str:
    print("选择一个选项:")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option.display_label}")

    values = {option.value: option.value for option in options}
    values.update({str(index): option.value for index, option in enumerate(options, 1)})
    while True:
        raw = input(f"输入编号或名称 [{default}]: ").strip()
        if not raw:
            return default
        selected = values.get(raw.lower())
        if selected is not None:
            return selected
        print("无效选项，请输入列表中的编号或名称。")
