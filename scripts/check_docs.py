#!/usr/bin/env python3
"""Validate NaumiAgent documentation classification, links, and current commands."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ALLOWED_STATUSES = frozenset(
    {"current", "product_spec", "historical", "migration", "reference", "quality"}
)

INLINE_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>\n]+>|[^\s)]+)",
    re.MULTILINE,
)
REFERENCE_LINK_RE = re.compile(
    r"^\s{0,3}\[[^\]\n]+\]:\s*(?P<target><[^>\n]+>|\S+)",
    re.MULTILINE,
)
FENCE_RE = re.compile(r"^\s{0,3}(?P<marker>`{3,}|~{3,})")

RETIRED_COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("naumi chat --classic", re.compile(r"\bnaumi\s+chat\s+--classic\b")),
    (
        "python -m naumi_agent.main chat",
        re.compile(r"\bpython(?:3)?\s+-m\s+naumi_agent\.main\s+chat\b"),
    ),
    (
        "naumi ui --legacy（独立推荐命令）",
        re.compile(r"(?m)^\s*(?:[$>]\s*)?naumi\s+ui\s+--legacy\s*$"),
    ),
)


@dataclass(frozen=True, slots=True)
class GovernanceRule:
    pattern: str
    status: str
    enforce_current: bool = False


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[str, ...]
    document_count: int
    status_counts: dict[str, int]


def _manifest_error(detail: str) -> str:
    return f"治理清单无效：{detail}。请修正 docs/governance.json。"


def _load_manifest(path: Path) -> tuple[tuple[GovernanceRule, ...], tuple[str, ...]]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (), (_manifest_error(f"找不到文件 {path}"),)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return (), (_manifest_error(f"无法读取 JSON：{exc}"),)

    if not isinstance(payload, dict):
        return (), (_manifest_error("根节点必须是 JSON object"),)
    if payload.get("version") != 1:
        return (), (_manifest_error("version 必须为 1"),)

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        return (), (_manifest_error("rules 必须是非空数组"),)

    rules: list[GovernanceRule] = []
    errors: list[str] = []
    for index, raw_rule in enumerate(raw_rules):
        prefix = f"rules[{index}]"
        if not isinstance(raw_rule, dict):
            errors.append(_manifest_error(f"{prefix} 必须是 object"))
            continue

        unknown = set(raw_rule) - {"pattern", "status", "enforce_current"}
        if unknown:
            errors.append(_manifest_error(f"{prefix} 包含未知字段 {sorted(unknown)}"))

        pattern = raw_rule.get("pattern")
        status = raw_rule.get("status")
        enforce_current = raw_rule.get("enforce_current", status == "current")
        if not isinstance(pattern, str) or not pattern.strip():
            errors.append(_manifest_error(f"{prefix}.pattern 必须是非空字符串"))
        if status not in ALLOWED_STATUSES:
            errors.append(
                _manifest_error(
                    f"{prefix}.status 必须是 {sorted(ALLOWED_STATUSES)} 之一"
                )
            )
        if not isinstance(enforce_current, bool):
            errors.append(_manifest_error(f"{prefix}.enforce_current 必须是 boolean"))
        if enforce_current is True and status != "current":
            errors.append(
                _manifest_error(f"{prefix} 只有 current 状态可以启用 enforce_current")
            )
        if enforce_current is False and status == "current":
            errors.append(
                _manifest_error(f"{prefix} 的 current 状态必须启用 enforce_current")
            )

        if (
            isinstance(pattern, str)
            and pattern.strip()
            and status in ALLOWED_STATUSES
            and isinstance(enforce_current, bool)
            and not (enforce_current and status != "current")
            and not (not enforce_current and status == "current")
        ):
            rules.append(
                GovernanceRule(
                    pattern=pattern,
                    status=status,
                    enforce_current=enforce_current,
                )
            )

    if errors:
        return (), tuple(errors)
    return tuple(rules), ()


def _discover_documents(root: Path) -> tuple[Path, ...]:
    documents: list[Path] = []
    readme = root / "README.md"
    if readme.is_file():
        documents.append(readme)
    docs_root = root / "docs"
    if docs_root.is_dir():
        documents.extend(path for path in docs_root.rglob("*.md") if path.is_file())
    return tuple(sorted(documents, key=lambda path: path.relative_to(root).as_posix()))


def _without_fenced_code(text: str) -> str:
    output: list[str] = []
    active_character: str | None = None
    active_length = 0
    for line in text.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group("marker")
            if active_character is None:
                active_character = marker[0]
                active_length = len(marker)
                output.append("\n" if line.endswith("\n") else "")
                continue
            if marker[0] == active_character and len(marker) >= active_length:
                active_character = None
                active_length = 0
                output.append("\n" if line.endswith("\n") else "")
                continue
        if active_character is None:
            output.append(line)
        else:
            output.append("\n" if line.endswith("\n") else "")
    return "".join(output)


def _without_inline_code(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            output.append(text[index])
            index += 1
            continue

        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and text[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2:
            output.append(text[index])
            index += 1
            continue

        opening_end = index
        while opening_end < len(text) and text[opening_end] == "`":
            opening_end += 1
        marker_length = opening_end - index

        search = opening_end
        closing_start: int | None = None
        closing_end: int | None = None
        while search < len(text) and text[search] != "\n":
            if text[search] != "`":
                search += 1
                continue
            run_end = search
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            if run_end - search == marker_length:
                closing_start = search
                closing_end = run_end
                break
            search = run_end

        if closing_start is None or closing_end is None:
            output.append(text[index:opening_end])
            index = opening_end
            continue

        output.append(" " * (closing_end - index))
        index = closing_end

    return "".join(output)


def _link_targets(text: str) -> tuple[str, ...]:
    visible_text = _without_inline_code(_without_fenced_code(text))
    targets = [match.group("target") for match in INLINE_LINK_RE.finditer(visible_text)]
    targets.extend(match.group("target") for match in REFERENCE_LINK_RE.finditer(visible_text))
    return tuple(targets)


def _local_target(source: Path, raw_target: str, root: Path) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = unquote(target.replace(r"\(", "(").replace(r"\)", ")"))
    if not target or target.startswith(("#", "//", "/")):
        return None

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None

    resolved = (source.parent / parsed.path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    return resolved


def _line_number(text: str, needle: str) -> int:
    index = text.find(needle)
    return text.count("\n", 0, max(index, 0)) + 1


def validate_repository(root: Path, manifest_path: Path | None = None) -> ValidationReport:
    root = root.resolve()
    manifest = (manifest_path or root / "docs" / "governance.json").resolve()
    documents = _discover_documents(root)
    rules, manifest_errors = _load_manifest(manifest)
    if manifest_errors:
        return ValidationReport(
            errors=manifest_errors,
            document_count=len(documents),
            status_counts={},
        )

    errors: list[str] = []
    classified: dict[Path, GovernanceRule] = {}
    for document in documents:
        relative = document.relative_to(root).as_posix()
        matches = [rule for rule in rules if fnmatch.fnmatchcase(relative, rule.pattern)]
        if not matches:
            errors.append(f"{relative}：未分类；请在 docs/governance.json 中添加唯一规则。")
            continue
        if len(matches) > 1:
            patterns = ", ".join(rule.pattern for rule in matches)
            errors.append(f"{relative}：命中多个分类规则（{patterns}）；请消除规则重叠。")
            continue
        classified[document] = matches[0]

    for document, rule in classified.items():
        relative = document.relative_to(root).as_posix()
        try:
            text = document.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative}：无法读取 UTF-8 文档：{exc}")
            continue

        for raw_target in _link_targets(text):
            target = _local_target(document, raw_target, root)
            if target is None or target.exists():
                continue
            try:
                display_target = target.relative_to(root).as_posix()
            except ValueError:
                display_target = str(target)
            line = _line_number(text, raw_target)
            errors.append(
                f"{relative}:{line}：本地链接目标不存在：{display_target}"
            )

        if rule.enforce_current:
            for label, pattern in RETIRED_COMMANDS:
                match = pattern.search(text)
                if match:
                    line = text.count("\n", 0, match.start()) + 1
                    errors.append(
                        f"{relative}:{line}：当前文档仍包含退役入口 {label}；"
                        "请改用 naumi 或 naumi tui。"
                    )

    counts = Counter(rule.status for rule in classified.values())
    return ValidationReport(
        errors=tuple(errors),
        document_count=len(documents),
        status_counts=dict(sorted(counts.items())),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 NaumiAgent 文档治理规则")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="仓库根目录（默认由脚本路径推导）",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="治理清单路径（默认 docs/governance.json）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate_repository(args.root, args.manifest)
    if report.errors:
        print(
            f"文档治理检查失败：{len(report.errors)} 个问题，"
            f"扫描 {report.document_count} 份文档。",
            file=sys.stderr,
        )
        for error in report.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    summary = "、".join(
        f"{status}={count}" for status, count in report.status_counts.items()
    )
    print(
        f"文档治理检查通过：扫描 {report.document_count} 份文档"
        f"（{summary or '无文档'}）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
