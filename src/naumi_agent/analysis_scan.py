"""Deterministic static-analysis scans exposed as a plain CLI.

Usage::

    python -m naumi_agent.analysis_scan --mode chaos --target <path> [--qps N]

Prints one JSON object on stdout (report text included) so external hosts —
e.g. the pi coding-agent extension — can consume NaumiAgent's static
scanners without importing the engine or calling any model.  Exit codes:
0 success, 2 no usable target files, 3 unknown mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from naumi_agent.tools.analysis_common import read_sources, resolve_target
from naumi_agent.tools.analysis_support.static_modes import (
    scan_chaos,
    scan_scale,
    scan_state,
)

MODES = ("chaos", "scale", "state")


def run_scan(mode: str, target: str, qps: int) -> dict:
    """Run one deterministic scan and return a JSON-serializable payload."""
    files = resolve_target(target)
    source = read_sources(files)
    if mode == "chaos":
        report = scan_chaos(files, source)
    elif mode == "scale":
        report = scan_scale(files, source, qps)
    elif mode == "state":
        report = scan_state(files, source)
    else:
        raise ValueError(f"未知扫描模式: {mode}")
    return {
        "ok": True,
        "mode": mode,
        "target": target,
        "qps": qps if mode == "scale" else None,
        "files_scanned": len(files),
        "source_chars": len(source),
        "report": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m naumi_agent.analysis_scan",
        description="NaumiAgent 确定性静态扫描（无模型调用）",
    )
    parser.add_argument("--mode", required=True, choices=MODES, help="扫描模式")
    parser.add_argument("--target", required=True, help="文件、目录或 glob 路径")
    parser.add_argument(
        "--qps", type=int, default=1000, help="scale 模式的目标 QPS（默认 1000）"
    )
    args = parser.parse_args(argv)

    target_path = Path(args.target).expanduser()
    if not target_path.exists():
        print(
            json.dumps(
                {"ok": False, "error": f"目标路径不存在: {args.target}"}, ensure_ascii=False
            ),
            flush=True,
        )
        return 2

    try:
        payload = run_scan(args.mode, args.target, args.qps)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 3

    if payload["files_scanned"] == 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "目标路径下没有可扫描的源码文件"
                    "（支持扩展名见 analysis_common.SOURCE_EXTENSIONS）",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
