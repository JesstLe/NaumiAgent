"""Deterministic helpers for authorized hook/instrumentation analysis."""

from __future__ import annotations

import re

_TARGET_TYPES = {
    "native_cpp": [
        (r"(?:C\+\+|cpp|native|unreal engine|directx|vulkan)", "原生 C/C++ 编译"),
        (r"(?:\.exe|\.dll|\.so|\.dylib|\.sys)", "原生二进制文件"),
        (r"(?:3A|AAA|unreal|虚幻)", "3A/虚幻引擎目标"),
    ],
    "dotnet": [
        (r"(?:C#|csharp|\.NET|unity|mono|il2cpp)", ".NET/C#/Unity 平台"),
        (r"(?:assembly-csharp|dnspy|ilspy)", ".NET 反编译特征"),
    ],
    "java": [
        (r"(?:java|kotlin|android|apk|dex)", "Java/Android 平台"),
        (r"(?:jadx|smali|dalvik)", "Android 逆向特征"),
    ],
    "wasm": [
        (r"(?:wasm|webassembly|emscripten)", "WebAssembly"),
        (r"(?:\.wasm|wasm2wat)", "WASM 文件"),
    ],
}

_ANTI_DEBUG_PATTERNS = [
    (r"(?:anti.?cheat|EAC|BattlEye|VAC|Easy.?Anti)", "商业反作弊/完整性系统"),
    (r"(?:Themida|VMProtect|Enigma|UPX|ASPack)", "加壳/混淆保护"),
    (r"(?:IsDebuggerPresent|NtQueryInformationProcess)", "反调试 API"),
    (r"(?:integrity.?check|signature.?verify)", "完整性校验"),
    (r"(?:kernel.?driver|ring.?0|驱动)", "内核级保护"),
]

_FORMAT_HINTS = {
    "native_cpp": ["MZ/PE", "ELF", "Mach-O"],
    "dotnet": ["MZ/PE + CLR metadata", "Assembly-CSharp.dll", "global-metadata.dat"],
    "java": ["DEX", "APK/ZIP", "JAR/ZIP"],
    "wasm": ["WebAssembly magic"],
}


def scan_hook(task: str) -> str:
    """Classify target and report instrumentation risk."""
    findings: list[str] = []
    target_matches = classify_target_types(task)

    if target_matches:
        findings.append("- 目标平台:")
        for ttype, label in target_matches:
            findings.append(f"  - {ttype}: {label}")
    else:
        findings.append("- 目标平台: 未明确指定（先做只读 inventory，不猜测 ABI/offset）")

    findings.append("- 推荐侦测顺序:")
    for step in build_recon_steps([ttype for ttype, _ in target_matches]):
        findings.append(f"  - {step}")

    anti_debug = detect_protection_indicators(task)
    if anti_debug:
        findings.append(f"- 保护/合规风险: {len(anti_debug)} 种")
        for item in anti_debug:
            findings.append(f"  - {item}")
        findings.append("  -> 先确认授权与测试环境，默认不提供绕过或规避步骤")
    else:
        findings.append("- 保护/合规风险: 未提及（仍需在 inventory 阶段验证）")

    complexity = len({ttype for ttype, _ in target_matches}) * 10 + len(anti_debug) * 15
    level = (
        "EXTREME" if complexity > 50
        else "HIGH" if complexity > 30
        else "MEDIUM" if complexity > 10
        else "LOW"
    )
    findings.append(f"- 逆向复杂度: {complexity} ({level})")

    return "\n".join(findings)


def classify_target_types(task: str) -> list[tuple[str, str]]:
    """Return matching target runtime types."""
    task_lower = task.lower()
    target_matches: list[tuple[str, str]] = []
    for ttype, patterns in _TARGET_TYPES.items():
        for pattern, label in patterns:
            if re.search(pattern, task_lower, re.IGNORECASE):
                target_matches.append((ttype, label))
                break
    return target_matches


def detect_protection_indicators(task: str) -> list[str]:
    """Return mentioned anti-debug/anti-tamper indicators."""
    task_lower = task.lower()
    return [
        label
        for pattern, label in _ANTI_DEBUG_PATTERNS
        if re.search(pattern, task_lower, re.IGNORECASE)
    ]


def build_recon_steps(target_types: list[str]) -> list[str]:
    """Build conservative reconnaissance steps from detected target types."""
    unique_types = list(dict.fromkeys(target_types)) or ["unknown"]
    steps: list[str] = [
        "读取文件头、大小、样本 hash，确认真实格式。",
        "枚举公开符号/导出表/manifest/metadata，禁止猜测函数名和 offset。",
    ]
    for target_type in unique_types:
        if target_type == "native_cpp":
            steps.extend(
                [
                    "优先解析 PE/ELF/Mach-O 导出与依赖库，再决定是否需要动态观测。",
                    "只在授权测试环境中规划 Detours/MinHook/Frida 观测点。",
                ]
            )
        elif target_type == "dotnet":
            steps.extend(
                [
                    "先用 metadata/程序集清单确认类型和方法名。",
                    "优先生成 Harmony/反射观测计划，不修改业务状态。",
                ]
            )
        elif target_type == "java":
            steps.extend(
                [
                    "先解析 APK/JAR/DEX manifest 与包名。",
                    "优先生成 Frida/Xposed 只读参数记录计划。",
                ]
            )
        elif target_type == "wasm":
            steps.extend(
                [
                    "先验证 wasm magic、导入表和导出表。",
                    "优先使用浏览器 DevTools/WASI 日志观测调用边界。",
                ]
            )
        else:
            steps.append("目标类型未知时先运行只读 inventory 脚本，不进入注入设计。")
    return steps


def build_hook_inventory_script(task: str, target_type: str = "") -> str:
    """Build a dependency-free read-only inventory script."""
    target_types = [ttype for ttype, _ in classify_target_types(f"{task} {target_type}")]
    protections = detect_protection_indicators(f"{task} {target_type}")
    return f'''\
"""Read-only target inventory script generated by analysis_hook."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TASK = {task!r}
TARGET_TYPE_HINT = {target_type!r}
TARGET_TYPES = {target_types!r}
PROTECTION_HINTS = {protections!r}
SAMPLE_BYTES = 1024 * 1024


def detect_format(header: bytes, suffix: str) -> str:
    if header.startswith(b"MZ"):
        return "pe_or_dotnet"
    if header.startswith(b"\\x7fELF"):
        return "elf"
    if header[:4] in (b"\\xfe\\xed\\xfa\\xce", b"\\xfe\\xed\\xfa\\xcf", b"\\xcf\\xfa\\xed\\xfe"):
        return "mach_o"
    if header.startswith(b"dex\\n"):
        return "dex"
    if header.startswith(b"\\x00asm"):
        return "wasm"
    if header.startswith(b"PK\\x03\\x04") and suffix in {{".apk", ".jar", ".zip"}}:
        return "zip_container"
    return "unknown"


def inspect_file(path: Path) -> dict[str, object]:
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            header = handle.read(64)
            handle.seek(0)
            sample = handle.read(SAMPLE_BYTES)
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    return {{
        "path": str(path),
        "found": True,
        "size": stat.st_size,
        "suffix": path.suffix.lower(),
        "format": detect_format(header, path.suffix.lower()),
        "sha256_first_1m": hashlib.sha256(sample).hexdigest(),
    }}


def inspect_target(raw: str) -> list[dict[str, object]]:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        return [{{"path": str(path), "found": False, "error": "not exists"}}]
    if path.is_file():
        return [inspect_file(path)]
    if path.is_dir():
        results = []
        for child in list(path.rglob("*"))[:500]:
            if child.is_file() and child.suffix.lower() in {{
                ".exe", ".dll", ".so", ".dylib", ".apk", ".jar", ".dex", ".wasm",
            }}:
                results.append(inspect_file(child))
            if len(results) >= 80:
                break
        return results
    return [{{"path": str(path), "found": False, "error": "unsupported path type"}}]


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        files.extend(inspect_target(target))
    return {{
        "status": "ok" if targets else "awaiting_targets",
        "task": TASK,
        "target_type_hint": TARGET_TYPE_HINT,
        "target_types": TARGET_TYPES,
        "protection_hints": PROTECTION_HINTS,
        "files": files,
        "next_step": (
            "基于 files[].format 和真实文件 hash 选择观测点；"
            "没有授权前不要注入、patch 或规避保护。"
        ),
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_hook_report(task: str, target_type: str, scan_evidence: str) -> str:
    """Build deterministic hook analysis output."""
    combined = f"{task} {target_type}".strip()
    matched_types = [ttype for ttype, _ in classify_target_types(combined)]
    format_hints = _format_hints_for(matched_types)
    script = build_hook_inventory_script(task, target_type)
    return (
        "## Hook 确定性合规侦测方案\n"
        "- 安全边界: 只读 inventory、授权优先、默认不注入、不 patch、不绕过保护。\n"
        f"- 目标格式提示: {', '.join(format_hints)}\n\n"
        f"## 静态侦测扫描\n{scan_evidence}\n\n"
        "## Read-only Target Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python hook_inventory.py <binary_or_directory>`。\n"
        "- 输出 `files[].format`、`size`、`sha256_first_1m`，作为后续观测点选择证据。\n"
        "- 对 APK/JAR/DEX/WASM/PE/ELF/Mach-O 只做文件头与样本 hash 读取。\n\n"
        "## 插桩设计边界\n"
        "- UNKNOWN ABI、UNKNOWN symbol、UNKNOWN offset 必须保持 UNKNOWN。\n"
        "- 先从公开导出、manifest、metadata、日志和调试符号建立观测点。\n"
        "- 只有在明确授权的测试环境中，才把 Frida/Harmony/MinHook 作为观测实现。\n"
        "- 发现反作弊、完整性校验、内核驱动时，默认停止并要求授权确认。\n\n"
        "## 回填模板\n"
        "- `files[].format`: 真实目标格式。\n"
        "- `files[].sha256_first_1m`: 样本指纹，用于确认后续分析对象一致。\n"
        "- `target_types`: 任务文本推断出的运行时类型；必须由真实文件格式修正。\n"
        "- `protection_hints`: 文本中提及的保护风险，不等于已验证事实。\n"
    )


def _format_hints_for(target_types: list[str]) -> list[str]:
    hints: list[str] = []
    for target_type in target_types or ["native_cpp", "dotnet", "java", "wasm"]:
        hints.extend(_FORMAT_HINTS.get(target_type, []))
    return list(dict.fromkeys(hints))

