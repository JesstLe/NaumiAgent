#!/usr/bin/env python3
"""Verify that a frozen backend can create Engine, emit ready, and stop on EOF."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix="naumi-frozen-bridge-") as workspace:
        completed = subprocess.run(
            [str(binary), "__ui-bridge", "--config", ".naumi/config.yaml"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0:
        raise SystemExit(
            "冻结 Bridge 退出异常："
            f"code={completed.returncode}\n{completed.stderr[-2000:]}"
        )
    if not lines:
        raise SystemExit(f"冻结 Bridge 未输出 ready。\n{completed.stderr[-2000:]}")
    try:
        record = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"冻结 Bridge 首行不是 JSON：{lines[0][:500]}") from exc
    if record.get("type") != "ready":
        raise SystemExit(f"冻结 Bridge 首事件不是 ready：{record.get('type')}")
    print(
        json.dumps(
            {
                "ok": True,
                "component": "naumi-frozen-bridge",
                "first_event": "ready",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
