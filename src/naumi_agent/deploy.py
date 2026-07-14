"""Deployment bootstrap and validation helpers."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path
from naumi_agent.config.settings import AppConfig


@dataclass
class ValidationReport:
    ok: bool
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def emit(self) -> None:
        for message in self.messages:
            print(message)
        for error in self.errors:
            print(error, file=sys.stderr)


def _required_paths(config: AppConfig) -> list[tuple[str, Path]]:
    workspace_root = config.resolve_workspace_root()
    session_parent = Path(config.memory.session_db_path).expanduser().parent
    vector_root = Path(config.memory.vector_db_path).expanduser()
    if not session_parent.is_absolute():
        session_parent = Path.cwd() / session_parent
    if not vector_root.is_absolute():
        vector_root = Path.cwd() / vector_root
    return [
        ("workspace_root", workspace_root),
        ("session_db_dir", session_parent.resolve()),
        ("vector_db_dir", vector_root.resolve()),
    ]


def validate_deployment(
    config_path: str | Path,
    *,
    create_dirs: bool = False,
    require_api_key: bool = False,
) -> ValidationReport:
    """Validate the deployment config and optionally create runtime directories."""
    report = ValidationReport(ok=True)
    path = Path(resolve_config_path(config_path)).expanduser()
    if not path.exists() or not path.is_file():
        report.ok = False
        report.errors.append(f"配置文件不存在: {path}")
        return report

    config = AppConfig.from_yaml(path)
    report.messages.append(f"已加载配置: {path.resolve()}")

    if require_api_key and not config.models.api_key:
        report.ok = False
        report.errors.append(
            "缺少模型 API Key。请设置 NAUMI_MODELS__API_KEY，"
            "或通过首次引导保存到系统凭据库。"
        )

    for label, required_path in _required_paths(config):
        if required_path.exists():
            report.messages.append(f"目录可用: {label}={required_path}")
            continue
        if create_dirs:
            try:
                required_path.mkdir(parents=True, exist_ok=True)
                report.messages.append(f"已创建目录: {label}={required_path}")
            except OSError as exc:
                report.ok = False
                report.errors.append(f"无法创建目录: {label}={required_path} ({exc})")
        else:
            report.ok = False
            report.errors.append(f"目录不存在: {label}={required_path}")

    if config.api.api_keys:
        report.messages.append("API 鉴权已启用: 需要 X-API-Key 或 api_key 查询参数。")
    else:
        report.messages.append(
            "API 鉴权未启用: 适合本机或受信任内网，公开部署前请配置 api.api_keys。"
        )

    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naumi-deploy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="校验部署配置和运行目录")
    validate.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径",
    )
    validate.add_argument("--create-dirs", action="store_true", help="缺失目录时自动创建")
    validate.add_argument("--require-api-key", action="store_true", help="缺少模型 API Key 时失败")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        report = validate_deployment(
            args.config,
            create_dirs=args.create_dirs,
            require_api_key=args.require_api_key,
        )
        report.emit()
        return 0 if report.ok else 1

    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
