"""Shared Unicode terminal-width contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from rich.cells import cell_len

from naumi_agent.ui.terminal_width import (
    display_width,
    pad_or_truncate,
    strip_terminal_sequences,
    truncate_to_width,
    wrap_to_width,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "terminal-ui"
    / "terminal-width-contract.json"
)
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_shared_width_contract_measurements() -> None:
    for fixture in _CONTRACT["measurement_cases"]:
        assert display_width(fixture["text"]) == fixture["width"], fixture["id"]


def test_textual_rich_cell_measurement_matches_shared_contract() -> None:
    for fixture in _CONTRACT["measurement_cases"]:
        assert cell_len(fixture["text"]) == fixture["width"], fixture["id"]


def test_shared_width_contract_truncation_is_grapheme_atomic() -> None:
    for fixture in _CONTRACT["truncation_cases"]:
        actual = truncate_to_width(fixture["text"], fixture["width"])
        assert actual == fixture["expected"], fixture["id"]
        assert display_width(actual) <= fixture["width"], fixture["id"]


def test_shared_width_contract_wrap_is_bounded() -> None:
    for fixture in _CONTRACT["wrap_cases"]:
        actual = wrap_to_width(fixture["text"], fixture["width"])
        assert actual == fixture["expected"], fixture["id"]
        assert all(display_width(line) <= fixture["width"] for line in actual), fixture["id"]


def test_terminal_controls_do_not_consume_visible_cells() -> None:
    styled = "\x1b[31m红色\x1b[0m\x1b]8;;https://example.test\x07链接\x1b]8;;\x07"

    assert strip_terminal_sequences(styled) == "红色链接"
    assert display_width(styled) == 8
    assert truncate_to_width(styled, 5) == "红色…"


def test_numeric_zero_is_measured_as_visible_text() -> None:
    assert display_width(0) == 1


def test_pad_or_truncate_occupies_exact_cells() -> None:
    for text, width in (("hi", 6), ("你好世界", 5), ("A👩‍💻B", 3), ("你", 1), ("x", 0)):
        result = pad_or_truncate(text, width)
        assert display_width(result) == width
