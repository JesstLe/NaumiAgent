"""Cross-runtime verification for the shared terminal width contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from naumi_agent.ui.terminal_width import (
    display_width,
    truncate_to_width,
    wrap_to_width,
)

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = json.loads(
    (_ROOT / "frontend/terminal-ui/terminal-width-contract.json").read_text(
        encoding="utf-8"
    )
)


def test_python_and_node_apply_the_same_width_contract() -> None:
    script = """
import { readFileSync } from "node:fs";
import {
  truncatePlain,
  visibleWidth,
  wrapAnsiLine,
} from "./frontend/terminal-ui/src/ansi.js";
const contract = JSON.parse(
  readFileSync("./frontend/terminal-ui/terminal-width-contract.json", "utf8"),
);
process.stdout.write(JSON.stringify({
  widths: contract.measurement_cases.map((item) => visibleWidth(item.text)),
  truncations: contract.truncation_cases.map(
    (item) => truncatePlain(item.text, item.width),
  ),
  wraps: contract.wrap_cases.map(
    (item) => wrapAnsiLine(item.text, item.width),
  ),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    node = json.loads(result.stdout)
    python = {
        "widths": [
            display_width(item["text"]) for item in _CONTRACT["measurement_cases"]
        ],
        "truncations": [
            truncate_to_width(item["text"], item["width"])
            for item in _CONTRACT["truncation_cases"]
        ],
        "wraps": [
            wrap_to_width(item["text"], item["width"])
            for item in _CONTRACT["wrap_cases"]
        ],
    }

    assert node == python
