"""Bounded symbol-level diff for governed Claude source mappings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.baseline import observe_source_baseline
from naumi_agent.claude_source.refresh import (
    SourceRefreshStore,
    resolve_claude_source_db_path,
)
from naumi_agent.claude_source.structural_diff import (
    SourceStructuralDiff,
    SourceTreeChange,
    build_source_structural_diff,
)

SymbolKind = Literal["component", "event", "export", "keybinding"]
SymbolChangeKind = Literal["added", "modified", "moved", "removed"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_KEY_LITERAL_RE = re.compile(
    r"^(?:(?:ctrl|control|shift|alt|option|meta|cmd|command)\+)+"
    r"(?:[a-z0-9]|escape|enter|return|tab|space|backspace|delete|up|down|left|right)$",
    re.IGNORECASE,
)
_SIMPLE_KEY_NAMES = frozenset(
    {
        "backspace",
        "delete",
        "down",
        "enter",
        "escape",
        "left",
        "return",
        "right",
        "space",
        "tab",
        "up",
    }
)
_EVENT_SYMBOL_RE = re.compile(
    r"(?:Event|EventType|EventName|Action|ActionType|MessageType|Notification"
    r"|_EVENT|_ACTION|_MESSAGE)$"
)
_EVENT_LITERAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{1,127}$")
_KEY_SYMBOL_RE = re.compile(r"(?:Binding|Bindings|Keybinding|Keybindings|Shortcut|Shortcuts)$")
_SOURCE_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
_MAX_MAPPING_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_MAPPED_PATHS = 512
_MAX_SYMBOLS_PER_FILE = 2_000
_MAX_CHANGES = 10_000
_MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSymbolChange(_StrictModel):
    change_kind: SymbolChangeKind
    symbol_kind: SymbolKind
    name: str = Field(min_length=1, max_length=256)
    areas: tuple[str, ...] = Field(min_length=1, max_length=64)
    old_path: str = ""
    new_path: str = ""
    baseline_signature_sha256: str = ""
    current_signature_sha256: str = ""

    @field_validator("old_path", "new_path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _normalize_path(value) if value else ""

    @field_validator("baseline_signature_sha256", "current_signature_sha256")
    @classmethod
    def _optional_digest(cls, value: str) -> str:
        if value and not _SHA256_RE.fullmatch(value):
            raise ValueError("symbol signature digest 必须是完整小写 SHA-256。")
        return value

    @field_validator("areas")
    @classmethod
    def _areas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("symbol areas 必须排序且不得重复。")
        return value

    @model_validator(mode="after")
    def _shape(self) -> SourceSymbolChange:
        if self.change_kind == "added":
            valid = bool(
                self.new_path
                and self.current_signature_sha256
                and not self.old_path
                and not self.baseline_signature_sha256
            )
        elif self.change_kind == "removed":
            valid = bool(
                self.old_path
                and self.baseline_signature_sha256
                and not self.new_path
                and not self.current_signature_sha256
            )
        else:
            valid = bool(
                self.old_path
                and self.new_path
                and self.baseline_signature_sha256
                and self.current_signature_sha256
            )
            if self.change_kind == "moved":
                valid = valid and self.old_path != self.new_path
            elif self.change_kind == "modified":
                valid = valid and (
                    self.baseline_signature_sha256 != self.current_signature_sha256
                    or self.old_path != self.new_path
                )
        if not valid:
            raise ValueError("symbol change 字段组合无效。")
        return self


class SourceSymbolDiff(_StrictModel):
    """Integrity-bound, read-only symbol comparison receipt."""

    schema_version: Literal[1] = 1
    symbol_diff_id: str
    structural_diff_id: str
    baseline_entry_id: str
    observation_id: str
    baseline_commit: str
    current_commit: str
    includes_worktree: bool
    status: Literal["unchanged", "change_detected", "invalid"]
    analyzed_file_count: int = Field(ge=0, le=_MAX_MAPPED_PATHS * 2)
    baseline_symbol_count: int = Field(ge=0, le=_MAX_CHANGES)
    current_symbol_count: int = Field(ge=0, le=_MAX_CHANGES)
    changes: tuple[SourceSymbolChange, ...] = Field(max_length=_MAX_CHANGES)
    counts: dict[str, int]
    affected_areas: tuple[str, ...] = Field(max_length=512)
    risk_flags: tuple[
        Literal[
            "component_removed",
            "event_contract_changed",
            "export_removed",
            "keybinding_changed",
            "mapped_symbol_scope_incomplete",
        ],
        ...,
    ]
    errors: tuple[str, ...] = Field(max_length=20)

    @field_validator(
        "symbol_diff_id",
        "structural_diff_id",
        "baseline_entry_id",
        "observation_id",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("symbol diff digest 必须是完整小写 SHA-256。")
        return value

    @field_validator("affected_areas", "risk_flags")
    @classmethod
    def _ordered_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("symbol diff 集合必须排序且不得重复。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> SourceSymbolDiff:
        ordered = tuple(
            sorted(
                self.changes,
                key=lambda item: (
                    item.symbol_kind,
                    item.name,
                    item.new_path,
                    item.old_path,
                    item.change_kind,
                ),
            )
        )
        if self.changes != ordered or len(self.changes) != len(set(self.changes)):
            raise ValueError("symbol changes 必须稳定排序且不得重复。")
        expected_counts = dict(
            sorted(Counter(item.change_kind for item in self.changes).items())
        )
        if self.counts != expected_counts:
            raise ValueError("symbol diff counts 与 changes 不一致。")
        if self.status == "unchanged" and (self.changes or self.risk_flags or self.errors):
            raise ValueError("unchanged symbol diff 不得携带变化、风险或错误。")
        if self.status == "change_detected" and not self.changes:
            raise ValueError("change_detected symbol diff 必须携带变化。")
        if self.status == "invalid" and not self.errors:
            raise ValueError("invalid symbol diff 必须说明错误。")
        if self.status != "invalid" and self.errors:
            raise ValueError("非 invalid symbol diff 不得携带错误。")
        if self.symbol_diff_id != source_symbol_diff_sha256(self):
            raise ValueError("symbol diff digest 不一致。")
        return self


@dataclass(frozen=True)
class _Token:
    kind: Literal["identifier", "number", "punctuation", "string"]
    value: str
    line: int


@dataclass(frozen=True)
class _Symbol:
    kind: SymbolKind
    name: str
    path: str
    signature_sha256: str


def build_source_symbol_diff(
    store: SourceRefreshStore,
    *,
    manifest_path: str | Path,
    source_root: str | Path,
    project_root: str | Path,
) -> SourceSymbolDiff:
    """Compare governed mapped symbols without fetching or writing either repo."""
    structural = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source_root,
        project_root=project_root,
    )
    if structural.status == "invalid":
        return _invalid_receipt(
            structural,
            errors=structural.errors or ("source structural diff 无法验证。",),
        )
    if structural.mapping_status != "current":
        return _invalid_receipt(
            structural,
            errors=("source mapping 不是已批准 current 状态，不能确定符号范围。",),
        )

    observation = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source_root,
        project_root=project_root,
    )
    root = Path(source_root).expanduser().resolve()
    project = Path(project_root).expanduser().resolve()
    try:
        mapping = _load_mapping(
            project,
            observation.mapping.path,
            expected_sha256=observation.mapping.sha256,
        )
        baseline, current, analyzed_count = _capture_mapped_symbols(
            root,
            structural,
            mapping,
        )
        rename_targets = _rename_targets(structural.changes)
        area_by_path = _area_by_path(mapping)
        for old_path, new_path in rename_targets.items():
            if old_path in area_by_path:
                area_by_path[new_path] = area_by_path[old_path]
        changes = _compare_symbols(
            baseline,
            current,
            rename_targets=rename_targets,
            area_by_path=area_by_path,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _invalid_receipt(structural, errors=(str(exc),))

    affected_areas = tuple(
        sorted({area for change in changes for area in change.areas})
    )
    risks: set[str] = set()
    if any(
        item.symbol_kind == "export" and item.change_kind == "removed"
        for item in changes
    ):
        risks.add("export_removed")
    if any(
        item.symbol_kind == "component" and item.change_kind == "removed"
        for item in changes
    ):
        risks.add("component_removed")
    if any(item.symbol_kind == "event" for item in changes):
        risks.add("event_contract_changed")
    if any(item.symbol_kind == "keybinding" for item in changes):
        risks.add("keybinding_changed")
    mapped_structural_paths = {
        path
        for impact in structural.mapped_impacts
        for path in impact.source_paths
    }
    symbol_paths = {item.old_path or item.new_path for item in changes}
    if mapped_structural_paths - symbol_paths and changes:
        risks.add("mapped_symbol_scope_incomplete")
    status: Literal["unchanged", "change_detected"] = (
        "change_detected" if changes else "unchanged"
    )
    return _build_receipt(
        structural,
        status=status,
        analyzed_file_count=analyzed_count,
        baseline_symbol_count=len(baseline),
        current_symbol_count=len(current),
        changes=changes,
        affected_areas=affected_areas,
        risk_flags=tuple(sorted(risks)),
        errors=(),
    )


def source_symbol_diff_sha256(receipt: SourceSymbolDiff) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("symbol_diff_id", None)
    return _sha256_json(payload)


def _capture_mapped_symbols(
    root: Path,
    structural: SourceStructuralDiff,
    mapping: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[_Symbol, ...], tuple[_Symbol, ...], int]:
    mapped_paths = tuple(
        sorted({path for _area, paths in mapping for path in paths})
    )
    if len(mapped_paths) > _MAX_MAPPED_PATHS:
        raise ValueError(
            f"source symbol diff 映射路径超过 {_MAX_MAPPED_PATHS} 条安全上限。"
        )
    renames = _rename_targets(structural.changes)
    baseline: list[_Symbol] = []
    current: list[_Symbol] = []
    analyzed = 0
    for old_path in mapped_paths:
        if PurePosixPath(old_path).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        baseline_blob = _git_blob(root, structural.baseline_commit, old_path)
        if baseline_blob is None:
            raise ValueError(f"已批准 commit 缺少 mapped source：{old_path}")
        baseline.extend(_extract_symbols(baseline_blob, old_path))
        analyzed += 1
        current_path = renames.get(old_path, old_path)
        current_blob = _current_blob(
            root,
            structural,
            current_path,
        )
        if current_blob is not None:
            current.extend(_extract_symbols(current_blob, current_path))
            analyzed += 1
    if len(baseline) > _MAX_CHANGES or len(current) > _MAX_CHANGES:
        raise ValueError(f"source symbol 数量超过 {_MAX_CHANGES} 条安全上限。")
    return (
        tuple(sorted(set(baseline), key=_symbol_key)),
        tuple(sorted(set(current), key=_symbol_key)),
        analyzed,
    )


def _compare_symbols(
    baseline: tuple[_Symbol, ...],
    current: tuple[_Symbol, ...],
    *,
    rename_targets: dict[str, str],
    area_by_path: dict[str, tuple[str, ...]],
) -> tuple[SourceSymbolChange, ...]:
    current_by_key = {
        (item.path, item.kind, item.name): item
        for item in current
    }
    consumed: set[tuple[str, SymbolKind, str]] = set()
    changes: list[SourceSymbolChange] = []
    for before in baseline:
        target_path = rename_targets.get(before.path, before.path)
        key = (target_path, before.kind, before.name)
        after = current_by_key.get(key)
        areas = tuple(
            sorted(
                set(area_by_path.get(before.path, ()))
                | set(area_by_path.get(target_path, ()))
            )
        )
        if not areas:
            areas = ("unmapped",)
        if after is None:
            changes.append(
                SourceSymbolChange(
                    change_kind="removed",
                    symbol_kind=before.kind,
                    name=before.name,
                    areas=areas,
                    old_path=before.path,
                    baseline_signature_sha256=before.signature_sha256,
                )
            )
            continue
        consumed.add(key)
        if before.path != after.path and before.signature_sha256 == after.signature_sha256:
            changes.append(
                SourceSymbolChange(
                    change_kind="moved",
                    symbol_kind=before.kind,
                    name=before.name,
                    areas=areas,
                    old_path=before.path,
                    new_path=after.path,
                    baseline_signature_sha256=before.signature_sha256,
                    current_signature_sha256=after.signature_sha256,
                )
            )
        elif before.signature_sha256 != after.signature_sha256:
            changes.append(
                SourceSymbolChange(
                    change_kind="modified",
                    symbol_kind=before.kind,
                    name=before.name,
                    areas=areas,
                    old_path=before.path,
                    new_path=after.path,
                    baseline_signature_sha256=before.signature_sha256,
                    current_signature_sha256=after.signature_sha256,
                )
            )
    for after in current:
        key = (after.path, after.kind, after.name)
        if key in consumed:
            continue
        areas = area_by_path.get(after.path, ("unmapped",))
        changes.append(
            SourceSymbolChange(
                change_kind="added",
                symbol_kind=after.kind,
                name=after.name,
                areas=areas,
                new_path=after.path,
                current_signature_sha256=after.signature_sha256,
            )
        )
    deduped = set(changes)
    if len(deduped) > _MAX_CHANGES:
        raise ValueError(f"source symbol diff 超过 {_MAX_CHANGES} 条变化上限。")
    return tuple(
        sorted(
            deduped,
            key=lambda item: (
                item.symbol_kind,
                item.name,
                item.new_path,
                item.old_path,
                item.change_kind,
            ),
        )
    )


def _extract_symbols(blob: bytes, path: str) -> tuple[_Symbol, ...]:
    if len(blob) > _MAX_SOURCE_BYTES:
        raise ValueError(
            f"mapped source 超过 {_MAX_SOURCE_BYTES} bytes 安全上限：{path}"
        )
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"mapped source 不是 UTF-8：{path}") from exc
    try:
        tokens = _tokenize(text)
    except ValueError as exc:
        raise ValueError(f"{exc}（{path}）") from exc
    symbols: set[_Symbol] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "identifier" and token.value == "export":
            extracted, next_index = _export_symbols(tokens, index, path)
            symbols.update(extracted)
            index = max(index + 1, next_index)
            continue
        if (
            token.kind == "identifier"
            and token.value
            in {
                "addEventListener",
                "dispatch",
                "emit",
                "logEvent",
                "publishEvent",
                "removeEventListener",
                "send",
                "trackEvent",
            }
            and index + 2 < len(tokens)
            and tokens[index + 1].value == "("
            and tokens[index + 2].kind == "string"
        ):
            literal = tokens[index + 2].value
            if _looks_like_event(literal, caller=token.value):
                symbols.add(
                    _symbol(
                        "event",
                        literal,
                        path,
                        ("event", literal),
                    )
                )
        index += 1
    if _is_keybinding_path(path):
        for token in tokens:
            if token.kind == "string" and _looks_like_keybinding(token.value):
                normalized = token.value.strip().lower()
                symbols.add(
                    _symbol(
                        "keybinding",
                        normalized,
                        path,
                        ("key", normalized),
                    )
                )
    if len(symbols) > _MAX_SYMBOLS_PER_FILE:
        raise ValueError(
            f"mapped source 单文件符号超过 {_MAX_SYMBOLS_PER_FILE} 条安全上限：{path}"
        )
    return tuple(sorted(symbols, key=_symbol_key))


def _export_symbols(
    tokens: tuple[_Token, ...],
    start: int,
    path: str,
) -> tuple[tuple[_Symbol, ...], int]:
    index = start + 1
    if index >= len(tokens):
        return (), index
    if tokens[index].value == "default":
        index += 1
    if index < len(tokens) and tokens[index].value == "async":
        index += 1
    if index >= len(tokens):
        return (), index
    token = tokens[index]
    names: list[tuple[str, tuple[str, ...]]] = []
    if token.value == "{":
        index += 1
        while index < len(tokens) and tokens[index].value != "}":
            current = tokens[index]
            if current.kind == "identifier":
                if (
                    current.value == "type"
                    and index + 1 < len(tokens)
                    and tokens[index + 1].kind == "identifier"
                ):
                    index += 1
                    current = tokens[index]
                exported = current.value
                signature = ["named", current.value]
                if (
                    index + 2 < len(tokens)
                    and tokens[index + 1].value == "as"
                    and tokens[index + 2].kind == "identifier"
                ):
                    exported = tokens[index + 2].value
                    signature.extend(("as", exported))
                    index += 2
                names.append((exported, tuple(signature)))
            index += 1
        return _symbols_for_exports(path, names), index + 1
    if token.value in {
        "class",
        "const",
        "enum",
        "function",
        "interface",
        "let",
        "type",
        "var",
    }:
        declaration = token.value
        index += 1
        if index < len(tokens) and tokens[index].kind == "identifier":
            name = tokens[index].value
            signature = _declaration_signature(
                tokens,
                index - 1,
                declaration=declaration,
            )
            names.append((name, (declaration, *signature)))
            return _symbols_for_exports(path, names), index + 1
    if token.kind == "identifier":
        names.append((token.value, ("default", token.value)))
        return _symbols_for_exports(path, names), index + 1
    return (), index + 1


def _symbols_for_exports(
    path: str,
    names: list[tuple[str, tuple[str, ...]]],
) -> tuple[_Symbol, ...]:
    symbols: set[_Symbol] = set()
    for name, signature in names:
        if not _IDENTIFIER_RE.fullmatch(name):
            continue
        symbols.add(_symbol("export", name, path, signature))
        declaration = signature[0] if signature else ""
        component_declaration = declaration in {
            "class",
            "const",
            "default",
            "function",
            "let",
            "var",
        } or (
            declaration == "named"
            and PurePosixPath(path).suffix.lower() in {".jsx", ".tsx"}
        )
        if name[:1].isupper() and component_declaration:
            symbols.add(_symbol("component", name, path, signature))
        if _EVENT_SYMBOL_RE.search(name):
            symbols.add(_symbol("event", name, path, signature))
        if _KEY_SYMBOL_RE.search(name):
            symbols.add(_symbol("keybinding", name, path, signature))
    return tuple(symbols)


def _declaration_signature(
    tokens: tuple[_Token, ...],
    declaration_index: int,
    *,
    declaration: str,
) -> tuple[str, ...]:
    signature: list[str] = []
    depth = 0
    brace_depth = 0
    saw_body = False
    for token in tokens[declaration_index : declaration_index + 512]:
        value = token.value
        if value in {"(", "[", "<"}:
            depth += 1
        elif value in {")", "]", ">"}:
            depth = max(0, depth - 1)
        if depth == 0 and value == "{":
            if declaration in {"enum", "interface"}:
                brace_depth += 1
                saw_body = True
            else:
                break
        elif saw_body and depth == 0 and value == "}":
            brace_depth -= 1
        if (
            depth == 0
            and declaration in {"const", "let", "var"}
            and value == "=>"
        ):
            signature.append(value)
            break
        if depth == 0 and not saw_body and value == ";":
            break
        signature.append(value)
        if saw_body and brace_depth == 0:
            break
    return tuple(signature)


def _tokenize(text: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    line = 1
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            line += char == "\n"
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = length if end < 0 else end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("mapped source 包含未终止块注释。")
            line += text[index : end + 2].count("\n")
            index = end + 2
            continue
        if char == "/" and _slash_starts_regex(tokens):
            start_line = line
            regex_end = _regex_literal_end(text, index)
            if regex_end is not None:
                index = regex_end
                tokens.append(_Token("punctuation", "/regex/", start_line))
                continue
        if char in {"'", '"', "`"}:
            quote = char
            start_line = line
            index += 1
            value: list[str] = []
            while index < length:
                current = text[index]
                if current == "\\":
                    if index + 1 >= length:
                        raise ValueError("mapped source 包含未终止转义字符串。")
                    escaped = text[index + 1]
                    value.append(escaped)
                    line += escaped == "\n"
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                value.append(current)
                line += current == "\n"
                index += 1
            else:
                raise ValueError("mapped source 包含未终止字符串。")
            tokens.append(_Token("string", "".join(value), start_line))
            continue
        if char.isalpha() or char in {"_", "$"}:
            end = index + 1
            while end < length and (
                text[end].isalnum() or text[end] in {"_", "$"}
            ):
                end += 1
            tokens.append(_Token("identifier", text[index:end], line))
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < length and (text[end].isalnum() or text[end] in {"_", "."}):
                end += 1
            tokens.append(_Token("number", text[index:end], line))
            index = end
            continue
        matched = next(
            (
                punctuation
                for punctuation in (
                    "===",
                    "!==",
                    "=>",
                    "==",
                    "!=",
                    "&&",
                    "||",
                    "??",
                    "?.",
                    "...",
                )
                if text.startswith(punctuation, index)
            ),
            char,
        )
        tokens.append(_Token("punctuation", matched, line))
        index += len(matched)
    return tuple(tokens)


def _slash_starts_regex(tokens: list[_Token]) -> bool:
    if not tokens:
        return True
    previous = tokens[-1]
    if previous.kind == "identifier":
        return previous.value in {
            "case",
            "delete",
            "in",
            "instanceof",
            "of",
            "return",
            "throw",
            "typeof",
            "void",
            "yield",
        }
    return previous.value in {
        "!",
        "!=",
        "!==",
        "&&",
        "(",
        ",",
        ":",
        ";",
        "=",
        "==",
        "===",
        "=>",
        "?",
        "??",
        "[",
        "{",
        "||",
    }


def _regex_literal_end(text: str, start: int) -> int | None:
    index = start + 1
    in_character_class = False
    while index < len(text):
        current = text[index]
        if current == "\\":
            if index + 1 >= len(text):
                return None
            index += 2
            continue
        if current == "\n":
            return None
        if current == "[":
            in_character_class = True
        elif current == "]":
            in_character_class = False
        elif current == "/" and not in_character_class:
            index += 1
            while index < len(text) and text[index].isalpha():
                index += 1
            return index
        index += 1
    return None


def _load_mapping(
    project_root: Path,
    relative_path: str,
    *,
    expected_sha256: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = _safe_project_file(project_root, relative_path)
    raw = path.read_bytes()
    if len(raw) > _MAX_MAPPING_BYTES:
        raise ValueError("source mapping 超过安全上限。")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("source mapping 已漂移，不能确定符号范围。")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source mapping 不是有效 JSON。") from exc
    entries = document.get("mapping") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("source mapping 缺少 mapping 数组。")
    mapping: list[tuple[str, tuple[str, ...]]] = []
    seen_areas: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("source mapping entry 必须是对象。")
        area = entry.get("area")
        paths = entry.get("claude_code")
        if (
            not isinstance(area, str)
            or not area.strip()
            or area in seen_areas
            or not isinstance(paths, list)
            or not paths
            or any(not isinstance(item, str) for item in paths)
        ):
            raise ValueError("source mapping area/path 合同无效。")
        normalized = tuple(sorted({_normalize_path(item) for item in paths}))
        mapping.append((area, normalized))
        seen_areas.add(area)
    return tuple(sorted(mapping))


def _area_by_path(
    mapping: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, tuple[str, ...]]:
    values: dict[str, set[str]] = {}
    for area, paths in mapping:
        for path in paths:
            values.setdefault(path, set()).add(area)
    return {path: tuple(sorted(areas)) for path, areas in values.items()}


def _rename_targets(changes: tuple[SourceTreeChange, ...]) -> dict[str, str]:
    return {
        change.old_path: change.new_path
        for change in changes
        if change.kind == "renamed"
    }


def _current_blob(
    root: Path,
    structural: SourceStructuralDiff,
    path: str,
) -> bytes | None:
    if structural.includes_worktree:
        candidate = _safe_source_file(root, path)
        try:
            raw = candidate.read_bytes()
        except FileNotFoundError:
            return None
        if not candidate.is_file():
            return None
        return raw
    return _git_blob(root, structural.current_commit, path)


def _git_blob(root: Path, commit: str, path: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{path}"],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git symbol snapshot 执行失败。") from exc
    if result.returncode != 0:
        return None
    if len(result.stdout) > _MAX_GIT_OUTPUT_BYTES:
        raise ValueError(f"Git symbol snapshot 超过安全上限：{path}")
    return result.stdout


def _safe_project_file(root: Path, relative: str) -> Path:
    return _safe_source_file(root, relative)


def _safe_source_file(root: Path, relative: str) -> Path:
    normalized = _normalize_path(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("source symbol path 越过根目录边界。")
    return candidate


def _normalize_path(value: str) -> str:
    if not value or "\x00" in value or any(ord(char) < 32 for char in value):
        raise ValueError("source symbol path 含非法控制字符。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source symbol path 必须是规范相对路径。")
    return path.as_posix()


def _symbol(
    kind: SymbolKind,
    name: str,
    path: str,
    signature: tuple[str, ...],
) -> _Symbol:
    return _Symbol(
        kind=kind,
        name=name,
        path=path,
        signature_sha256=_sha256_json(signature),
    )


def _symbol_key(item: _Symbol) -> tuple[str, str, str, str]:
    return (item.path, item.kind, item.name, item.signature_sha256)


def _looks_like_event(value: str, *, caller: str) -> bool:
    stripped = value.strip()
    if not _EVENT_LITERAL_RE.fullmatch(stripped):
        return False
    if caller in {
        "addEventListener",
        "logEvent",
        "publishEvent",
        "removeEventListener",
        "trackEvent",
    }:
        return True
    return bool(
        "/" in stripped
        or ":" in stripped
        or stripped.endswith((".event", ".message", ".action"))
    )


def _is_keybinding_path(path: str) -> bool:
    lowered = path.lower()
    return any(token in lowered for token in ("binding", "keymap", "shortcut"))


def _looks_like_keybinding(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(
        _KEY_LITERAL_RE.fullmatch(normalized)
        or normalized in _SIMPLE_KEY_NAMES
    )


def _build_receipt(
    structural: SourceStructuralDiff,
    *,
    status: Literal["unchanged", "change_detected", "invalid"],
    analyzed_file_count: int,
    baseline_symbol_count: int,
    current_symbol_count: int,
    changes: tuple[SourceSymbolChange, ...],
    affected_areas: tuple[str, ...],
    risk_flags: tuple[str, ...],
    errors: tuple[str, ...],
) -> SourceSymbolDiff:
    values = {
        "schema_version": 1,
        "structural_diff_id": structural.diff_id,
        "baseline_entry_id": structural.baseline_entry_id,
        "observation_id": structural.observation_id,
        "baseline_commit": structural.baseline_commit,
        "current_commit": structural.current_commit,
        "includes_worktree": structural.includes_worktree,
        "status": status,
        "analyzed_file_count": analyzed_file_count,
        "baseline_symbol_count": baseline_symbol_count,
        "current_symbol_count": current_symbol_count,
        "changes": changes,
        "counts": dict(
            sorted(Counter(item.change_kind for item in changes).items())
        ),
        "affected_areas": affected_areas,
        "risk_flags": risk_flags,
        "errors": errors,
    }
    return SourceSymbolDiff(symbol_diff_id=_sha256_json(values), **values)


def _invalid_receipt(
    structural: SourceStructuralDiff,
    *,
    errors: tuple[str, ...],
) -> SourceSymbolDiff:
    return _build_receipt(
        structural,
        status="invalid",
        analyzed_file_count=0,
        baseline_symbol_count=0,
        current_symbol_count=0,
        changes=(),
        affected_areas=(),
        risk_flags=(),
        errors=errors[:20],
    )


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"{type(value).__name__} 无法序列化。")


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="比较已批准 Claude source baseline 与当前 mapped symbols"
    )
    parser.add_argument("--manifest", default="frontend/terminal-ui/cc-source-map.v2.json")
    parser.add_argument("--source", default="../claude-code")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--store", default=str(resolve_claude_source_db_path()))
    args = parser.parse_args()
    try:
        receipt = build_source_symbol_diff(
            SourceRefreshStore(args.store),
            manifest_path=args.manifest,
            source_root=args.source,
            project_root=args.project_root,
        )
    except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.status == "unchanged" else 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "SourceSymbolChange",
    "SourceSymbolDiff",
    "build_source_symbol_diff",
    "source_symbol_diff_sha256",
]
