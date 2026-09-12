"""Tests for the naumi-analysis pi extension and its wiring helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from naumi_agent.pi_engine.extension import (
    default_pi_env,
    resolve_default_extension_args,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extension_args_auto_load_when_present(tmp_path: Path) -> None:
    ext_dir = tmp_path / "pi_extensions"
    ext_dir.mkdir()
    (ext_dir / "naumi-analysis.js").write_text("export default () => {}", encoding="utf-8")
    args = resolve_default_extension_args(tmp_path, [])
    assert args[-2:] == ["-e", str(ext_dir / "naumi-analysis.js")]


def test_extension_args_respect_explicit_flags(tmp_path: Path) -> None:
    explicit = ["-e", "custom.js"]
    assert resolve_default_extension_args(REPO_ROOT, explicit) == explicit
    assert resolve_default_extension_args(REPO_ROOT, ["--no-extensions"]) == [
        "--no-extensions"
    ]
    assert resolve_default_extension_args(tmp_path, []) == []


def test_default_env_carries_python() -> None:
    env = default_pi_env({"ZAI_CODING_CN_API_KEY": "k"})
    assert env["NAUMI_PYTHON"] == sys.executable
    assert env["ZAI_CODING_CN_API_KEY"] == "k"


def test_node_selftest_loads_extension_and_runs_tools(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("本机未安装 Node.js")
    fixture = tmp_path / "service"
    fixture.mkdir()
    (fixture / "app.py").write_text(
        "import requests\n\n"
        "def get(uid):\n"
        "    try:\n"
        "        return requests.get(f'http://x/{uid}').json()\n"
        "    except:\n"
        "        return None\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            node,
            str(REPO_ROOT / "pi_extensions" / "selftest.mjs"),
            str(fixture),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**dict(__import__("os").environ), "NAUMI_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS naumi_chaos" in result.stdout
    assert "PASS naumi_scale" in result.stdout
    assert "PASS naumi_state" in result.stdout
    assert "ALL PASS" in result.stdout
