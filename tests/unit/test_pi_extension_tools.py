"""Tests for the naumi-analysis pi extension and its wiring helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from naumi_agent.pi_engine.extension import (
    DEFAULT_IDENTITY_PROMPT,
    default_pi_env,
    resolve_pi_cli_args,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extension_args_auto_load_when_present(tmp_path: Path) -> None:
    ext_dir = tmp_path / "pi_extensions"
    ext_dir.mkdir()
    (ext_dir / "naumi-analysis.js").write_text("export default () => {}", encoding="utf-8")
    args = resolve_pi_cli_args(tmp_path, [], "")
    assert args[-2:] == ["-e", str(ext_dir / "naumi-analysis.js")]


def test_extension_args_respect_explicit_flags(tmp_path: Path) -> None:
    explicit = ["-e", "custom.js"]
    assert resolve_pi_cli_args(REPO_ROOT, explicit, "") == explicit
    assert resolve_pi_cli_args(REPO_ROOT, ["--no-extensions"], "") == [
        "--no-extensions"
    ]
    assert resolve_pi_cli_args(tmp_path, [], "") == []


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


def test_identity_prompt_injected_by_default(tmp_path: Path) -> None:
    args = resolve_pi_cli_args(tmp_path, [], None)
    assert args[:2] == ["--append-system-prompt", DEFAULT_IDENTITY_PROMPT]


def test_identity_prompt_custom_and_disabled(tmp_path: Path) -> None:
    custom = resolve_pi_cli_args(tmp_path, [], "自定义身份")
    assert "自定义身份" in custom
    disabled = resolve_pi_cli_args(tmp_path, [], "")
    assert "--append-system-prompt" not in disabled


def test_identity_prompt_yields_to_user_flags(tmp_path: Path) -> None:
    args = resolve_pi_cli_args(
        tmp_path, ["--system-prompt", "用户自己的提示词"], None
    )
    assert DEFAULT_IDENTITY_PROMPT not in args
    appended = resolve_pi_cli_args(
        tmp_path, ["--append-system-prompt", "追加块"], None
    )
    assert appended.count("--append-system-prompt") == 1
