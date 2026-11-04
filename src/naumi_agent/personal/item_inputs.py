"""Resolve explicit time/policy and capture actual authorized item inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from naumi_agent.personal.artifact_boundary import is_protected
from naumi_agent.personal.artifact_models import is_text_artifact, normalize_refs
from naumi_agent.personal.models import PersonalAgentError

ITEM_TOOLS = frozenset({"personal_artifacts", "code_execute", "bash_run", "file_read", "read"})
ITEM_ARTIFACT_OPERATIONS = frozenset({"get", "read", "publish"})


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def resolve_schedule(value, now: datetime):
    value = dict(value or {"mode": "now"})
    if set(value) - {
        "mode",
        "local_time",
        "timezone",
        "fold",
        "misfire_policy",
        "grace_seconds",
    }:
        raise PersonalAgentError("invalid_input", "时间设置包含未支持的字段")
    mode = value.get("mode", "now")
    zone_name = value.get("timezone", "Asia/Shanghai")
    if mode not in {"now", "once"} or not isinstance(zone_name, str):
        raise PersonalAgentError("invalid_input", "请选择立即执行或一次性时间")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PersonalAgentError("invalid_timezone", "请选择有效的 IANA 时区") from exc
    fold = value.get("fold")
    if fold is not None and (type(fold) is not int or fold not in {0, 1}):
        raise PersonalAgentError("invalid_input", "重复时刻请选择较早或较晚的一次")
    if now.tzinfo is None:
        raise ValueError("Item clock must return an aware datetime")
    now = now.astimezone(UTC)
    if mode == "now":
        resolved = now
        local = now.astimezone(zone).replace(tzinfo=None)
        fold = now.astimezone(zone).fold
    else:
        try:
            local = datetime.fromisoformat(value.get("local_time", ""))
        except (ValueError, TypeError) as exc:
            raise PersonalAgentError("invalid_time", "请选择完整的本地日期和时间") from exc
        if local.tzinfo is not None:
            raise PersonalAgentError("invalid_time", "本地时间与时区请分别提供")
        candidates = {}
        for choice in (0, 1):
            utc = local.replace(tzinfo=zone, fold=choice).astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates[choice] = utc
        if not candidates:
            raise PersonalAgentError("nonexistent_time", "夏令时切换跳过了该时刻，请选择其他时间")
        if len(set(candidates.values())) > 1 and fold is None:
            raise PersonalAgentError("ambiguous_time", "该时刻出现两次，请选择较早或较晚的一次")
        resolved = candidates.get(fold if fold is not None else 0)
        if resolved is None or resolved < now:
            raise PersonalAgentError("past_time", "时间已经过去，请选择新时间或立即执行")
    misfire = value.get("misfire_policy", "run_once")
    grace = value.get("grace_seconds", 86400)
    if misfire not in {"run_once", "skip"} or type(grace) is not int or not 0 <= grace <= 604800:
        raise PersonalAgentError("invalid_input", "补执行策略或宽限期无效")
    return {
        "mode": mode,
        "local_time": local.isoformat(),
        "timezone": zone_name,
        "fold": fold,
        "due_at": resolved.isoformat(),
        "misfire_policy": misfire,
        "grace_seconds": grace,
    }


def resolve_policy(engine, value, profile):
    supplied = dict(value or {})
    keys = {
        "tool_names",
        "artifact_operations",
        "model_tier",
        "max_turns",
        "max_cost_microusd",
        "timeout_seconds",
    }
    if set(supplied) - keys:
        raise PersonalAgentError("invalid_input", "执行策略包含未支持的字段")
    available = set(profile.tool_names) if profile else set(engine.tool_registry.names)
    defaults = {"personal_artifacts", "code_execute"} & available
    tools = supplied.get("tool_names", sorted(defaults))
    if (
        not isinstance(tools, list)
        or any(not isinstance(t, str) for t in tools)
        or len(set(tools)) != len(tools)
        or not set(tools) <= ITEM_TOOLS & available
    ):
        raise PersonalAgentError("invalid_policy", "工具未分配给该 Agent，或不属于本次本地事项范围")
    operations = supplied.get("artifact_operations", sorted(ITEM_ARTIFACT_OPERATIONS))
    if (
        not isinstance(operations, list)
        or any(not isinstance(op, str) for op in operations)
        or not set(operations) <= ITEM_ARTIFACT_OPERATIONS
    ):
        raise PersonalAgentError("invalid_policy", "本次事项只读取已选资料并保存自己的报告")
    tier = supplied.get("model_tier", profile.model_tier if profile else "capable")
    if tier not in {"fast", "capable", "reasoning"} or (profile and tier != profile.model_tier):
        raise PersonalAgentError("invalid_policy", "请选择该 Agent 当前配置的模型级别")
    limits = {
        "max_turns": (12, 1, 100),
        "max_cost_microusd": (2000000, 0, 100000000),
        "timeout_seconds": (180, 1, 3600),
    }
    result = {
        "tool_names": sorted(tools),
        "artifact_operations": sorted(set(operations)),
        "model_tier": tier,
    }
    for field, (default, low, high) in limits.items():
        number = supplied.get(field, default)
        if type(number) is not int or not low <= number <= high:
            raise PersonalAgentError("invalid_policy", "执行次数、预算或超时超出有效范围")
        result[field] = number
    result["max_turns"] = min(result["max_turns"], engine.config.safety.max_turns)
    parent_budget = engine.config.safety.max_budget_usd
    if parent_budget is not None:
        result["max_cost_microusd"] = min(
            result["max_cost_microusd"],
            max(0, int(parent_budget * 1000000)),
        )
    return result


def checked_directory(engine, raw):
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise PersonalAgentError("invalid_input", "请选择实际资料目录")
    workspace = Path(engine.workspace_root).resolve()
    value = Path(raw).expanduser()
    if ".." in value.parts:
        raise PersonalAgentError("path_denied", "目录路径不包含上级跳转")
    lexical = value if value.is_absolute() else workspace / value
    resolved = lexical.resolve()
    if not resolved.is_relative_to(workspace) or is_protected(engine, resolved):
        raise PersonalAgentError("path_denied", "请选择当前工作区内的资料目录")
    current = lexical
    while True:
        if current.exists():
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PersonalAgentError("path_denied", "资料目录包含链接别名，请选择原始目录")
        if current == workspace or current.parent == current:
            break
        current = current.parent
    if not resolved.is_dir():
        raise PersonalAgentError("not_found", "资料目录不存在")
    return resolved


def normalize_directories(engine, directories):
    if not isinstance(directories, list) or len(directories) > 8:
        raise PersonalAgentError("invalid_input", "最多选择 8 个资料目录")
    result = []
    for entry in directories:
        if not isinstance(entry, dict) or set(entry) - {
            "path",
            "pattern",
            "max_files",
            "max_bytes",
        }:
            raise PersonalAgentError("invalid_input", "资料目录设置无效")
        path = checked_directory(engine, entry.get("path"))
        pattern = entry.get("pattern", "**/*")
        count, size = entry.get("max_files", 64), entry.get("max_bytes", 52428800)
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > 200
            or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or ":" in pattern
            or type(count) is not int
            or not 1 <= count <= 100
            or type(size) is not int
            or not 1 <= size <= 52428800
        ):
            raise PersonalAgentError("invalid_input", "目录筛选、文件数或大小上限无效")
        result.append(
            {"path": str(path), "pattern": pattern, "max_files": count, "max_bytes": size}
        )
    return result


def _files(engine, directories):
    captured, total, entries, seen = [], 0, 0, set()
    for spec in directories:
        root = checked_directory(engine, spec["path"])
        files, size = 0, 0
        # Walk without following links and reject them before glob matching.
        for parent, names, filenames in os.walk(root, followlinks=False):
            parent_path = Path(parent)
            if len(parent_path.relative_to(root).parts) > 32:
                raise PersonalAgentError("input_limit", "资料目录层级过深，请缩小范围")
            if parent_path.resolve() != parent_path or is_protected(engine, parent_path):
                raise PersonalAgentError("path_denied", "资料目录在扫描时发生了变化")
            for name in [*names, *filenames]:
                entries += 1
                if entries > 20000:
                    raise PersonalAgentError("input_limit", "目录条目过多，请缩小资料范围")
                path = Path(parent) / name
                info = path.lstat()
                if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise PersonalAgentError("path_denied", "资料目录中存在链接，未启动执行")
            for name in sorted(filenames):
                path = Path(parent) / name
                relative = path.relative_to(root).as_posix()
                matches = path.match(spec["pattern"]) or (
                    spec["pattern"].startswith("**/") and path.match(spec["pattern"][3:])
                )
                if not matches or path in seen:
                    continue
                if is_protected(engine, path) or getattr(path.stat(), "st_nlink", 1) != 1:
                    raise PersonalAgentError("path_denied", "筛选结果包含私有运行资料或链接文件")
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_size > 20971520:
                    raise PersonalAgentError("input_limit", "单份资料不能超过 20 MB")
                # Bind bytes to the lstat identity, never follow a switched file/parent alias.
                fd = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                )
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
                    if any(getattr(opened, key) != getattr(before, key) for key in fields):
                        raise PersonalAgentError("input_changed", "资料在读取前发生变化")
                    if path.resolve(strict=True) != path or is_protected(engine, path):
                        raise PersonalAgentError("path_denied", "资料路径在读取时发生变化")
                    data = stream.read(before.st_size + 1)
                    after_open = os.fstat(stream.fileno())
                after = path.lstat()
                if (
                    any(
                        getattr(before, key) != getattr(after, key)
                        or getattr(before, key) != getattr(after_open, key)
                        for key in fields
                    )
                    or len(data) != before.st_size
                    or path.resolve(strict=True) != path
                ):
                    raise PersonalAgentError("input_changed", "资料正在变化，请稳定文件后重新确认")
                files += 1
                size += len(data)
                total += len(data)
                if files > spec["max_files"] or size > spec["max_bytes"] or total > 52428800:
                    raise PersonalAgentError("input_limit", "资料超过所选文件数或总大小上限")
                seen.add(path)
                captured.append((spec["path"], relative, path.name, data))
    return sorted(captured, key=lambda row: (row[0], row[1]))


async def capture_inputs(engine, definition, occurrence_id):
    service, actor = engine.personal_artifacts, definition["assignee"]["id"]
    refs = normalize_refs(definition["inputs"]["artifact_refs"])
    refs = await service.authorize_refs(actor_id=actor, refs=refs)
    manifest, sections, total_bytes = [], [], 0
    for ref in refs:
        value, content = await service.read(
            actor_id=actor,
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
        total_bytes += len(content)
        if len(content) > 20971520 or total_bytes > 52428800:
            raise PersonalAgentError("input_limit", "所选资料超过单文件或总大小上限")
        manifest.append(
            {
                "source": "artifact",
                "artifact_ref": ref,
                "filename": value["artifact_id"] + value["suffix"],
                "bytes": len(content),
                "sha256": value["sha256"],
            }
        )
    try:
        captured = await asyncio.to_thread(_files, engine, definition["inputs"]["directories"])
    except OSError as exc:
        raise PersonalAgentError("input_changed", "资料读取失败或正在变化，请核对原目录") from exc
    if len(refs) + len(captured) > 64:
        raise PersonalAgentError("input_limit", "本次来源超过 64 份，请缩小目录或减少资料")
    if total_bytes + sum(len(row[3]) for row in captured) > 52428800:
        raise PersonalAgentError("input_limit", "本次资料总大小超过 50 MB")
    for directory, relative, filename, content in captured:
        identity = fingerprint({"directory": directory, "relative": relative})[:32]
        value = await service.publish(
            actor_id=actor,
            request_id=f"item-input-{occurrence_id}-{identity}",
            title=relative[-120:],
            kind="material",
            filename=filename,
            content=content,
            evidence={
                "source_kind": "personal_item_directory",
                "occurrence_id": occurrence_id,
                "relative_path": relative,
            },
        )
        ref = service.reference(value)
        refs.append(ref)
        manifest.append(
            {
                "source": "directory",
                "artifact_ref": ref,
                "filename": filename,
                "bytes": len(content),
                "sha256": value["sha256"],
                "directory_path": directory,
                "relative_path": relative,
            }
        )
    remaining = 60000
    for ref in refs:
        snapshot, content = await service.read(
            actor_id=actor,
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
        text = content.decode("utf-8-sig") if is_text_artifact(snapshot) else ""
        excerpt = text[: min(12000, remaining)]
        remaining -= len(excerpt)
        sections.append(
            {
                "ref": ref,
                "text": excerpt,
                "text_available": is_text_artifact(snapshot),
                "input_path": f"/inputs/{ref['artifact_id']}-v{ref['version']}{snapshot['suffix']}",
                "truncated": len(text) > len(excerpt),
            }
        )
    context = json.dumps(
        {"type": "authorized_item_materials", "sources": sections},
        ensure_ascii=False,
        sort_keys=True,
    )
    return refs, manifest, context
