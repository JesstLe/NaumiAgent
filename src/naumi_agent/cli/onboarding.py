"""首次启动引导 — 像 Claude Code 一样零配置 onboarding."""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from naumi_agent.config.configurator import PROVIDER_PROFILES
from naumi_agent.config.credentials import (
    CredentialStoreError,
    load_model_api_key,
    store_model_api_key,
)

console = Console()


_PROVIDER_NAMES = {
    "kimi": "Kimi Coding API",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}
_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    key: {
        "name": _PROVIDER_NAMES[key],
        "default_model": profile.default_model,
        "fast_model": profile.fast_model,
        "reasoning_model": profile.reasoning_model,
        "api_base": profile.api_base,
        "temperature": profile.temperature,
    }
    for key, profile in PROVIDER_PROFILES.items()
}
_PROVIDER_PRESETS["custom"] = {
        "name": "自定义 API",
        "default_model": "custom-model",
        "fast_model": "custom-model",
        "reasoning_model": "custom-model",
        "api_base": "",
        "temperature": 1.0,
    }

_PERMISSION_MODES = {
    "strict": "严格 — 每次文件/Shell 操作都需确认",
    "moderate": "适中 — 写操作和敏感命令需确认",
    "relaxed": "宽松 —  mostly 自动执行",
}


def run_onboarding(config_path: Path, *, project_root: Path | None = None) -> bool:
    """运行交互式首次配置引导。返回是否成功创建配置文件。"""
    config_path = config_path.resolve()
    project_root = (project_root or config_path.parent).resolve()

    console.print(
        Panel.fit(
            "欢迎使用 NaumiAgent — 通用智能 Agent\n"
            "接下来需要配置模型密钥和基本偏好。",
            title="🚀 首次启动",
            border_style="cyan",
        )
    )

    # 1. Python 版本检查
    if sys.version_info < (3, 12):  # noqa: UP036
        console.print(
            f"[red]当前 Python {sys.version_info.major}.{sys.version_info.minor} "
            "不满足要求，请升级到 Python 3.12+ 后重试。[/red]"
        )
        return False

    # 2. 选择 provider
    provider = _choose_provider()
    preset = dict(_PROVIDER_PRESETS[provider])

    # 3. API Key
    api_key = _prompt_api_key(preset["name"])
    if not api_key:
        console.print(
            "[yellow]未提供 API Key，跳过配置。"
            "后续可运行 naumi configure，或设置 NAUMI_MODELS__API_KEY。[/yellow]"
        )
        return False

    environment_key = os.environ.get("NAUMI_MODELS__API_KEY", "")
    if environment_key != api_key:
        try:
            store_model_api_key(api_key, provider=provider)
        except (CredentialStoreError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            return False
        os.environ["NAUMI_MODELS__API_KEY"] = api_key
        console.print("[green]模型凭据已保存到系统安全存储。[/green]")

    # 4. 自定义 base URL / model
    if provider == "custom":
        preset["api_base"] = Prompt.ask("API Base URL", default="http://localhost:8000/v1")
        preset["default_model"] = Prompt.ask("默认模型", default=preset["default_model"])
        preset["fast_model"] = Prompt.ask("快速模型", default=preset["default_model"])
        preset["reasoning_model"] = Prompt.ask("推理模型", default=preset["default_model"])

    # 5. Workspace
    workspace_default = str(Path.cwd())
    workspace = Prompt.ask(
        "工作区目录（文件/Shell 工具默认作用范围）",
        default=workspace_default,
    )

    # 6. Permission mode
    permission_mode = Prompt.ask(
        "权限模式",
        choices=list(_PERMISSION_MODES.keys()),
        default="moderate",
    )

    # 7. 写入配置
    config_data = _build_config(
        provider=provider,
        preset=preset,
        workspace=workspace,
        permission_mode=permission_mode,
    )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, sort_keys=False, allow_unicode=True)

    console.print(f"[green]✅ 配置已写入 {config_path}[/green]")

    # 8. Node UI 检查
    _check_node_ui(project_root)
    _report_search_readiness()

    return True


def _choose_provider() -> str:
    choices = list(_PROVIDER_PRESETS.keys())
    console.print("\n[bold]选择模型提供商:[/bold]")
    for idx, key in enumerate(choices, 1):
        preset = _PROVIDER_PRESETS[key]
        console.print(f"  {idx}. {preset['name']}")

    selection = Prompt.ask(
        "输入编号或名称",
        choices=choices,
        default="kimi",
    )
    return selection


def _prompt_api_key(provider_name: str) -> str:
    """优先读取环境变量；否则交互式输入。"""
    env_key = os.environ.get("NAUMI_MODELS__API_KEY", "")
    if env_key:
        masked = f"{env_key[:4]}...{env_key[-4:]}" if len(env_key) >= 8 else "***"
        console.print(f"检测到环境变量 NAUMI_MODELS__API_KEY: [dim]{masked}[/dim]")
        if Confirm.ask("是否使用此 Key", default=True):
            return env_key

    console.print(f"\n请输入 [bold]{provider_name}[/bold] 的 API Key")
    console.print("[dim]输入时不会显示，也可直接回车跳过[/dim]")
    key = getpass.getpass("API Key: ").strip()
    return key


def _build_config(
    provider: str,
    preset: dict[str, Any],
    workspace: str,
    permission_mode: str,
) -> dict[str, Any]:
    return {
        "models": {
            "provider": provider,
            "default_model": preset["default_model"],
            "fast_model": preset["fast_model"],
            "reasoning_model": preset["reasoning_model"],
            "max_tokens": 4096,
            "temperature": preset.get("temperature", 1.0),
            "api_base": preset["api_base"],
            "model_info": {
                preset["default_model"]: {"max_context": 256000},
            },
        },
        "memory": {
            "session_db_path": "data/sessions.db",
            "vector_db_path": "data/chroma",
            "compaction_threshold": 0.75,
        },
        "workspace_root": workspace,
        "safety": {
            "permission_mode": permission_mode,
            "allowed_dirs": [workspace],
            "max_budget_usd": 5.0,
            "max_turns": 30,
            "max_input_tokens": 500000,
        },
        "mcp": {"servers": {}},
        "api": {
            "host": "127.0.0.1",
            "port": 8765,
            "api_keys": [],
            "cors_origins": ["*"],
        },
        "browser_daemon": {
            "enabled": False,
            "base_url": "http://127.0.0.1:3005",
        },
        "log_level": "INFO",
    }


def _check_node_ui(project_root: Path) -> None:
    """检查 Node.js 环境，为新一代终端 UI 做准备。"""
    node = shutil.which("node")
    if not node:
        console.print(
            "\n[yellow]未检测到 Node.js，新一代终端 UI（naumi ui）不可用。[/yellow]"
        )
        console.print("可使用 naumi chat --classic 或 naumi ui --legacy 继续工作。")
        console.print("如需 Node UI，请安装 Node.js 20+ 后运行：")
        console.print(f"  [dim]cd {project_root / 'frontend' / 'terminal-ui'} && npm install[/dim]")
        return

    try:
        version = _run([node, "--version"]).strip()
        console.print(f"\n[green]检测到 Node.js {version}[/green]")
    except Exception:
        console.print("[yellow]检测到 node 但无法获取版本[/yellow]")
        return

    node_modules = project_root / "frontend" / "terminal-ui" / "node_modules"
    if not node_modules.exists():
        console.print("[dim]新一代终端 UI 依赖未安装。[/dim]")
        if Confirm.ask("是否现在安装 Node UI 依赖？", default=True):
            ui_dir = project_root / "frontend" / "terminal-ui"
            try:
                console.print("正在安装 npm 依赖...")
                result = shutil.which("npm")
                if result:
                    _run([result, "install"], cwd=str(ui_dir))
                    console.print("[green]Node UI 依赖安装完成[/green]")
                else:
                    console.print("[red]未找到 npm[/red]")
            except Exception as exc:
                console.print(f"[red]安装失败: {exc}[/red]")


def _report_search_readiness() -> None:
    """Explain that web search works without asking for another required key."""
    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        console.print(
            "\n[green]网络搜索：已增强（检测到 Brave Search 凭据）。[/green]"
        )
        return
    console.print("\n[green]网络搜索：可用（零配置，无需搜索 API Key）。[/green]")
    console.print(
        "[dim]系统会先使用免 Key 搜索，必要时自动回退到浏览器；"
        "BRAVE_SEARCH_API_KEY 仅是可选增强项。[/dim]"
    )


def _run(cmd: list[str], cwd: str | None = None) -> str:
    import subprocess

    return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.STDOUT, text=True)


def migrate_legacy_model_api_key(config_path: str | Path) -> bool:
    """Move a plaintext legacy model key into the system credential store."""
    path = Path(config_path).resolve()
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    models = data.get("models")
    if not isinstance(models, dict):
        return False
    api_key = models.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return False

    provider = models.get("provider")
    store_model_api_key(
        api_key,
        provider=provider if isinstance(provider, str) else None,
    )
    os.environ["NAUMI_MODELS__API_KEY"] = api_key
    del models["api_key"]

    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, sort_keys=False, allow_unicode=True)
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def needs_onboarding(config_path: str | Path) -> bool:
    """判断是否需要首次启动引导。"""
    p = Path(config_path).resolve()
    if not p.exists():
        return True
    # 文件存在但内容为空或没有 api_key 时也引导
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return True
    models = data.get("models", {})
    env_key = os.environ.get("NAUMI_MODELS__API_KEY", "")
    if env_key or models.get("api_key"):
        return False
    try:
        provider = models.get("provider")
        return load_model_api_key(
            provider=provider if isinstance(provider, str) else None,
        ) is None
    except CredentialStoreError:
        return True
