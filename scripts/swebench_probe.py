"""Lightweight SWE-bench probe driving the NaumiAgent pi engine.

Pulls SWE-bench_Lite instances, checks out the base commit via codeload
tarballs, verifies the environment with the gold patch, then lets the pi
engine (real spawn: identity prompt + bundled extension) attempt the fix
and runs the FAIL_TO_PASS tests independently.

Usage:
    uv run python scripts/swebench_probe.py --ids sympy__sympy-13480 ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from naumi_agent.config.settings import AppConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.pi_engine.web_facade import PiWebEngine

_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp/SWE-bench_Lite&config=default&split=test"
)


def load_instances(ids: list[str]) -> list[dict]:
    wanted = set(ids)
    found: dict[str, dict] = {}
    for offset in range(0, 300, 100):
        if len(found) == len(wanted):
            break
        with urllib.request.urlopen(f"{_ROWS_URL}&offset={offset}&length=100", timeout=30) as resp:
            payload = json.load(resp)
        for row in payload.get("rows", []):
            record = row["row"]
            if record["instance_id"] in wanted:
                record["FAIL_TO_PASS_LIST"] = json.loads(record["FAIL_TO_PASS"])
                found[record["instance_id"]] = record
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"未找到实例: {sorted(missing)}")
    return [found[i] for i in ids]


def fetch_repo_tarball(repo: str, commit: str, dest: Path) -> Path:
    url = f"https://codeload.github.com/{repo}/tar.gz/{commit}"
    archive = dest / "repo.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as resp, archive.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    with tarfile.open(archive) as tf:
        names = tf.getnames()
        root = dest / "repo"
        tf.extractall(root.parent)
        extracted = root.parent / names[0]
        extracted.rename(root)
    archive.unlink(missing_ok=True)
    return root


def apply_patch(repo_dir: Path, patch_text: str) -> bool:
    result = subprocess.run(
        ["git", "apply", "-"],
        cwd=repo_dir,
        input=patch_text,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"    [gold] git apply 失败: {result.stderr[:200]}")
        return False
    return True


def _instance_python() -> str:
    """Old codebases need an old interpreter; prefer a system Python 3.9."""
    candidates = ["/usr/bin/python3"]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                out = subprocess.run(
                    [candidate, "--version"], capture_output=True, text=True, timeout=10
                )
                if "Python 3.9" in (out.stdout + out.stderr):
                    return candidate
            except Exception:
                continue
    return sys.executable


def make_venv(dest: Path) -> Path:
    python_bin = _instance_python()
    subprocess.run(
        [python_bin, "-m", "venv", str(dest)],
        check=True,
        capture_output=True,
    )
    pip = str(dest / "bin" / "pip")
    subprocess.run(
        [pip, "install", "-q", "--upgrade", "pip"],
        check=True, capture_output=True, timeout=300,
    )
    subprocess.run(
        [pip, "install", "-q", "pytest", "mpmath", "setuptools"],
        check=True, capture_output=True, timeout=300,
    )
    return dest


def extract_test_files(patch_text: str) -> list[str]:
    """Pull modified test file paths out of a diff header."""
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            if path and path != "/dev/null":
                files.append(path)
    return [f for f in files if "test" in Path(f).name]


def run_tests(venv_python: Path, repo_dir: Path, tests: list[str],
              test_patch: str = "", timeout: int = 600):
    """Run FAIL_TO_PASS tests, matching bare names with -k when needed."""
    argv = [str(venv_python), "-m", "pytest", "-q", "--no-header",
            "-p", "no:cacheprovider"]
    if all("/" in t or "::" in t for t in tests):
        argv.extend(tests)
    else:
        files = extract_test_files(test_patch) or ["."]
        argv.extend(files)
        argv.extend(["-k", " or ".join(tests)])
    return subprocess.run(argv, cwd=repo_dir, capture_output=True,
                          text=True, timeout=timeout)


PROMPT_TEMPLATE = """你要修复当前仓库中的一个真实 issue。

## Issue 描述
{problem}

## 任务要求
1. 先运行测试复现失败（测试已就位）：
   `python -m pytest {tests} -x -q`
2. 定位并修改源码（最小改动，不要改测试文件，除非测试文件本身有语法错误）。
3. 再次运行同一测试命令，确认全部通过。
4. 用两句话总结你改了什么。

完成后即结束，不要展开无关工作。"""


async def probe_instance(instance: dict, workdir: Path, engine_timeout_s: int) -> dict:
    iid = instance["instance_id"]
    print(f"\n=== {iid} ===")
    base = workdir / iid
    base.mkdir(parents=True)
    tests = instance["FAIL_TO_PASS_LIST"]

    repo_dir = fetch_repo_tarball(instance["repo"], instance["base_commit"], base)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, capture_output=True)
    venv = make_venv(base / "venv")
    venv_python = venv / "bin" / "python"

    # 环境对照:gold 测试 + gold 修复 → 必须 PASS
    if not apply_patch(repo_dir, instance["test_patch"]):
        return {"id": iid, "stage": "env", "error": "test_patch apply 失败"}
    if not apply_patch(repo_dir, instance["patch"]):
        return {"id": iid, "stage": "env", "error": "gold patch apply 失败"}
    gold = run_tests(venv_python, repo_dir, tests, instance["test_patch"])
    if gold.returncode != 0:
        return {
            "id": iid,
            "stage": "env",
            "error": f"gold patch 后 F2P 仍失败: {gold.stdout[-300:]}",
        }
    print("  [gold] F2P 通过（环境健康）")

    # 引擎实测:干净源码 + 仅测试补丁
    shutil.rmtree(repo_dir)
    repo_dir = fetch_repo_tarball(instance["repo"], instance["base_commit"], base)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, capture_output=True)
    apply_patch(repo_dir, instance["test_patch"])

    config = AppConfig()
    config.engine.provider = "pi"
    config.engine.pi.provider = "zai-coding-cn"
    config.engine.pi.model = "glm-4.7"
    config.engine.pi.env = {"ZAI_CODING_CN_API_KEY": "{env:OPENAI_API_KEY}"}
    config.workspace_root = str(repo_dir)
    config.memory.session_db_path = str(base / "probe-sessions.db")
    store = SessionStore(config.memory)
    engine = PiWebEngine(config, store)
    await store.create_session(title=iid, engine="pi")
    sessions_page, _total = await store.list_sessions(page=1, page_size=1)
    await engine.load_session(sessions_page[0].id)

    prompt = PROMPT_TEMPLATE.format(
        problem=instance["problem_statement"][:6000], tests=" ".join(tests)
    )
    sink_events: list[str] = []

    class Sink:
        async def emit(self, event) -> None:
            sink_events.append(str(event.type))

    started = time.time()
    status = "timeout"
    response = ""
    error = ""
    try:
        result = await asyncio.wait_for(
            engine.run_streaming(prompt, Sink()), timeout=engine_timeout_s
        )
        status = result.status
        response = result.response[:500]
        error = result.error[:300]
    except TimeoutError:
        status = "timeout"
    finally:
        await engine.stop()
        await store.close()
    elapsed = int(time.time() - started)

    # 独立评估
    verdict = run_tests(venv_python, repo_dir, tests, instance["test_patch"])
    resolved = verdict.returncode == 0
    diff = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from collections import Counter

    report = {
        "id": iid,
        "stage": "engine",
        "engine_status": status,
        "resolved": resolved,
        "elapsed_s": elapsed,
        "tool_events": dict(Counter(s for s in sink_events if "tool" in s.lower())),
        "pytest_tail": verdict.stdout.strip().splitlines()[-1] if verdict.stdout.strip() else "",
        "changed_files": len(diff.splitlines()),
        "response": response,
        "error": error,
    }
    print(f"  引擎状态: {status} | 用时 {elapsed}s | F2P {'PASS ✅' if resolved else 'FAIL ❌'}")
    print(f"  工具事件: {report['tool_events']} | 改动文件数: {report['changed_files']}")
    return report


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--timeout", type=int, default=900, help="单实例引擎超时秒数")
    parser.add_argument("--workdir", default="/tmp/swebench-probe")
    args = parser.parse_args()

    if not (os.environ.get("ZAI_CODING_CN_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        raise SystemExit("缺少模型密钥(ZAI_CODING_CN_API_KEY 或 OPENAI_API_KEY)")

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    instances = load_instances(args.ids)

    reports = []
    for instance in instances:
        reports.append(await probe_instance(instance, workdir, args.timeout))

    out = workdir / "report.json"
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    resolved = sum(1 for r in reports if r.get("resolved"))
    print(f"\n=== 汇总: {resolved}/{len(reports)} resolved,报告: {out} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
