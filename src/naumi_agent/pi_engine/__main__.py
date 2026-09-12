"""Entry point: ``python -m naumi_agent.pi_engine [--config PATH]``.

The Node terminal UI spawns this module as its JSONL bridge subprocess when
``engine.provider`` is ``pi``.  Configuration comes from the shared
NaumiAgent config file (``engine`` section).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from naumi_agent.config.settings import AppConfig
from naumi_agent.log_setup import setup_logging
from naumi_agent.pi_engine.bridge import PiTerminalBridge, serve_stdio
from naumi_agent.pi_engine.env import resolve_env_refs


def _configure_stdio_utf8() -> None:
    # Windows consoles default to a legacy code page; force UTF-8 for JSONL.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in {"utf-8", "utf8"}:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NaumiAgent pi engine JSONL bridge")
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="配置文件路径",
    )
    args = parser.parse_args(argv)

    from naumi_agent.config.paths import resolve_config_path

    resolved = resolve_config_path(args.config)
    config = AppConfig.from_yaml(resolved)
    config.bind_runtime_workspace(Path.cwd())
    setup_logging(config.log_level)

    bridge = PiTerminalBridge(
        binary=config.engine.pi.binary,
        provider=config.engine.pi.provider,
        model=config.engine.pi.model,
        extra_args=config.engine.pi.extra_args,
        workspace_root=config.resolve_workspace_root(),
        env=resolve_env_refs(config.engine.pi.env),
        identity_prompt=getattr(config.engine.pi, "system_prompt_append", None),
    )
    # The status refresh inside start() emits immediately, so the writer must
    # be bound before pi is spawned.
    bridge.bind_writer(sys.stdout)
    try:
        await bridge.start()
    except Exception as exc:
        print(
            f"pi 引擎启动失败：{exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    try:
        await serve_stdio(bridge)
    finally:
        await bridge.shutdown()
    return 0


def main(argv: list[str] | None = None) -> None:
    _configure_stdio_utf8()
    raise SystemExit(asyncio.run(_amain(argv)))


if __name__ == "__main__":
    main()
