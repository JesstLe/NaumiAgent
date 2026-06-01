"""Deterministic self-healing diagnostics helpers."""

from __future__ import annotations

import re
from pathlib import Path


def scan_heal(files: list[Path], source_text: str, error_log: str) -> str:
    """Scan code and error logs for actionable self-heal evidence."""
    findings: list[str] = []

    error_types = re.findall(r"(\w+Error|\w+Exception)", error_log)
    error_types = list(set(error_types))
    if error_types:
        findings.append(f"- 错误类型: {', '.join(error_types)}")

    file_refs = re.findall(r'File "([^"]+)", line (\d+)', error_log)
    if file_refs:
        findings.append("- 错误栈追踪到的文件:")
        for filepath, lineno in file_refs[:10]:
            findings.append(f"  - {filepath}:{lineno}")

    try_count = len(re.findall(r"\btry\s*:", source_text))
    except_count = len(re.findall(r"\bexcept\s+", source_text))
    findings.append(f"- 错误处理: {try_count} 个 try 块, {except_count} 个 except 子句")

    bare = re.findall(r"except\s*:", source_text)
    if bare:
        findings.append(f"- ⚠️ 裸 except: {len(bare)} 处 (会吞掉所有异常)")

    silent_catch = re.findall(r"except[^:]*:\s*\n\s*pass", source_text)
    if silent_catch:
        findings.append(f"- ⚠️ 静默捕获 (except...pass): {len(silent_catch)} 处")

    logging_used = len(re.findall(r"(?:logger\.|logging\.|log\.)", source_text))
    if logging_used:
        findings.append(f"- 日志记录: {logging_used} 处引用")
    else:
        findings.append("- ⚠️ 未发现日志记录 (logger/logging)")

    findings.append(f"- 扫描文件: {len(files)} 个")

    return "\n".join(findings) if findings else "- 静态扫描未发现额外线索"


def build_heal_report(
    error_log: str,
    scan_evidence: str = "",
    files: list[Path] | None = None,
) -> str:
    """Build deterministic diagnosis, root-frame, and regression guidance."""
    error_type, error_message = extract_error_summary(error_log)
    frames = extract_traceback_frames(error_log)
    root_frame = frames[-1] if frames else None
    lines = [
        "## Heal 确定性诊断",
        f"- 错误类型：{error_type or '未识别'}",
    ]
    if error_message:
        lines.append(f"- 错误信息：{error_message}")
    if root_frame:
        file_path, line_no, func_name = root_frame
        lines.append(f"- 疑似根因位置：{file_path}:{line_no} in {func_name}()")
    if frames:
        lines.append("- 调用栈切片：")
        for file_path, line_no, func_name in frames[-5:]:
            lines.append(f"  - {file_path}:{line_no} in {func_name}()")
    if files:
        lines.append(f"- 已扫描相关文件：{len(files)} 个")
    if scan_evidence:
        lines.append("")
        lines.append("## Heal 静态扫描")
        lines.append(scan_evidence)
    lines.append("")
    lines.append("## 最小修复方向")
    lines.extend(heal_repair_guidance(error_type, error_message))
    lines.append("")
    lines.append("## 回归验证建议")
    lines.append("- 先写一个复现该错误输入的 pytest，用当前错误日志作为断言依据。")
    lines.append("- 修复后至少验证：原始失败输入、空输入、类型错误输入、正常输入。")
    lines.append("- 如果涉及外部依赖，补 timeout、重试上限和失败日志。")
    return "\n".join(lines)


def extract_error_summary(error_log: str) -> tuple[str, str]:
    """Extract the final exception type and message from an error log."""
    candidates = [
        line.strip()
        for line in error_log.strip().splitlines()
        if line.strip() and not line.lstrip().startswith("File ")
    ]
    for line in reversed(candidates):
        match = re.match(
            r"(?P<type>[A-Za-z_][\w.]*?(?:Error|Exception))(?::\s*(?P<msg>.*))?$",
            line,
        )
        if match:
            return match.group("type"), match.group("msg") or ""
    return "", ""


def extract_traceback_frames(error_log: str) -> list[tuple[str, int, str]]:
    """Extract traceback frames as file, line number, and function name."""
    frames: list[tuple[str, int, str]] = []
    for file_path, line_no, func_name in re.findall(
        r'File "([^"]+)", line (\d+), in ([^\n]+)',
        error_log,
    ):
        frames.append((file_path, int(line_no), func_name.strip()))
    return frames


def heal_repair_guidance(error_type: str, error_message: str) -> list[str]:
    """Return deterministic repair guidance based on exception class."""
    lower = error_type.lower()
    message = error_message.lower()
    if "keyerror" in lower:
        missing = error_message.strip("'\"") or "缺失 key"
        return [
            f"- 在读取字典字段 `{missing}` 前做显式存在性校验。",
            "- 不要用裸 except 吞掉 KeyError；应返回带上下文的业务错误。",
            "- 回归测试覆盖：缺失 key、key 为 None、正常 key。",
        ]
    if "attributeerror" in lower:
        return [
            "- 在访问属性前确认对象类型和 None 边界。",
            "- 如果对象来自外部输入，入口处先做 schema/类型校验。",
            "- 回归测试覆盖：None、缺失属性对象、正常对象。",
        ]
    if "typeerror" in lower:
        return [
            "- 明确函数入参类型，在边界入口拒绝错误类型。",
            "- 对可选参数提供默认值或早返回错误，而不是让内部表达式崩溃。",
            "- 回归测试覆盖：None、字符串/数字混用、正常类型。",
        ]
    if "filenotfounderror" in lower or "no such file" in message:
        return [
            "- 在打开文件前检查路径存在性，并给出用户可理解的错误。",
            "- 对用户传入路径执行 sandbox/路径归一化校验。",
            "- 回归测试覆盖：不存在路径、目录路径、正常文件路径。",
        ]
    if "jsondecodeerror" in lower:
        return [
            "- 在 json.loads 边界捕获 JSONDecodeError 并返回结构化错误。",
            "- 日志记录原始输入长度和来源，不记录敏感完整内容。",
            "- 回归测试覆盖：空字符串、非法 JSON、合法 JSON。",
        ]
    return [
        "- 先定位最后一个业务栈帧，把修复限制在错误进入边界附近。",
        "- 增加输入校验、明确错误信息和一条覆盖该 traceback 的回归测试。",
        "- 避免大重构；先做可证明的最小修复。",
    ]
