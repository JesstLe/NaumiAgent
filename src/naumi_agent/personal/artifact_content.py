"""Bounded Page contracts and previews over actual immutable artifact bytes."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any

from naumi_agent.personal.artifact_models import (
    PRIVATE_TEXT_MATERIAL_TYPES,
    normalize_refs,
    strict_json_loads,
)
from naumi_agent.personal.models import PersonalAgentError


def _invalid(message):
    return PersonalAgentError("invalid_input", message)


def _text(value, label, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise _invalid(f"{label}格式不正确")
    return value


def _keys(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise _invalid("文档包含未知字段或缺少必要内容")


def _list(value, maximum, label):
    if not isinstance(value, list) or len(value) > maximum:
        raise _invalid(f"{label}超过支持的范围")
    return value


def validate_widget(value: Any, depth=0):
    if depth > 2:
        raise _invalid("组件嵌套最多三层")
    common = {"version", "type", "title", "source"}
    fields = {
        "metrics": {"items"},
        "table": {"columns", "rows"},
        "chart": {"x", "series", "rows", "style"},
        "image": {"url", "caption"},
        "html": {"html", "height"},
        "tabs": {"tabs"},
    }
    if not isinstance(value, dict) or value.get("type") not in fields:
        raise _invalid("文档组件类型不受支持")
    _keys(value, common | fields[value["type"]], {"type", "title"})
    if value.get("version", 1) != 1 or isinstance(value.get("version"), bool):
        raise _invalid("文档组件版本不正确")
    _text(value["title"], "组件标题", 120)
    if "source" in value:
        _text(value["source"], "组件来源", 500)
    kind = value["type"]
    if kind == "metrics":
        for item in _list(value.get("items"), 12, "指标"):
            _keys(item, {"label", "value", "unit", "note"}, {"label", "value"})
            _text(item["label"], "指标名称", 80)
            v = item["value"]
            if (
                isinstance(v, bool)
                or not isinstance(v, (str, int, float))
                or isinstance(v, (int, float))
                and not math.isfinite(v)
            ):
                raise _invalid("指标值需要文本或有限数值")
            if isinstance(v, str):
                _text(v, "指标值", 120)
            for key, maximum in (("unit", 30), ("note", 300)):
                if key in item:
                    _text(item[key], "指标内容", maximum)
    elif kind in {"table", "chart"}:
        columns = _list(
            value.get("columns" if kind == "table" else "series"),
            20 if kind == "table" else 6,
            "字段",
        )
        if not columns:
            raise _invalid("数据字段不能为空")
        names = []
        for column in columns:
            _keys(column, {"key", "label"}, {"key", "label"})
            names.append(_text(column["key"], "字段名", 80))
            _text(column["label"], "字段标题", 80)
        if len(set(names)) != len(names):
            raise _invalid("数据字段不能重复")
        for row in _list(value.get("rows"), 2000 if kind == "table" else 300, "数据行"):
            if not isinstance(row, dict) or len(row) > 30:
                raise _invalid("数据行格式不正确")
            for cell in row.values():
                if cell is not None and not isinstance(cell, (bool, str, int, float)):
                    raise _invalid("数据单元格格式不正确")
                if (
                    isinstance(cell, str)
                    and len(cell) > 2000
                    or isinstance(cell, (int, float))
                    and not math.isfinite(cell)
                ):
                    raise _invalid("数据单元格过长或数值无效")
            if kind == "chart" and any(
                row.get(key) is not None
                and (isinstance(row[key], bool) or not isinstance(row[key], (int, float)))
                for key in names
            ):
                raise _invalid("图表系列需要数值")
        if kind == "chart":
            _text(value.get("x"), "横轴", 80)
            if value.get("style", "bar") not in {"bar", "line", "scatter"}:
                raise _invalid("图表样式不正确")
    elif kind == "image":
        url = _text(value.get("url"), "图片地址", 2048)
        if not re.match(r"^https?://", url, re.I):
            raise _invalid("文档图片需要明确的 HTTP 来源地址")
        if "caption" in value:
            _text(value["caption"], "图片说明", 500)
    elif kind == "html":
        _text(value.get("html"), "HTML", 100000)
        height = value.get("height", 420)
        if (
            isinstance(height, bool)
            or not isinstance(height, (int, float))
            or not math.isfinite(height)
            or not 240 <= height <= 900
        ):
            raise _invalid("HTML 展示高度需要 240 至 900")
    elif kind == "tabs":
        tabs = _list(value.get("tabs"), 8, "标签页")
        if not tabs:
            raise _invalid("标签页不能为空")
        for tab in tabs:
            _keys(tab, {"label", "content"}, {"label", "content"})
            _text(tab["label"], "标签", 50)
            validate_widget(tab["content"], depth + 1)
    return value


def validate_page(value: Any) -> dict:
    _keys(value, {"version", "type", "title", "sections"}, {"version", "type", "title", "sections"})
    if type(value["version"]) is not int or value["version"] != 1 or value["type"] != "page":
        raise _invalid("文档版本或类型不正确")
    _text(value["title"], "文档标题", 120)
    sections = _list(value["sections"], 40, "文档章节")
    if not sections:
        raise _invalid("文档需要至少一个章节")
    identities = []
    for section in sections:
        _keys(
            section,
            {"id", "title", "markdown", "widget", "evidence_status", "source_refs"},
            {"id", "title", "evidence_status"},
        )
        identity = _text(section["id"], "章节编号", 80)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity):
            raise _invalid("章节编号需要字母、数字或短横线")
        identities.append(identity)
        _text(section["title"], "章节标题", 120)
        if section["evidence_status"] not in {"fact", "estimate", "unknown"}:
            raise _invalid("章节需要区分事实、估算或待核对")
        if not {"markdown", "widget"} & section.keys():
            raise _invalid("章节需要正文或真实组件")
        if "markdown" in section:
            _text(section["markdown"], "章节正文", 100000, empty=True)
        if "widget" in section:
            validate_widget(section["widget"])
        normalize_refs(section.get("source_refs", []))
    if len(set(identities)) != len(identities):
        raise _invalid("章节编号不能重复")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 500000:
        raise _invalid("交互文档超过 500 KB，请拆分文档或返回数据文件")
    return value


def page_refs(content: bytes, suffix: str) -> list[dict]:
    if suffix != ".json":
        return []
    value = strict_json_loads(content)
    if isinstance(value, dict) and value.get("type") == "page":
        validate_page(value)
        return [ref for section in value["sections"] for ref in section.get("source_refs", [])]
    return []


def preview(content: bytes, suffix: str) -> dict:
    if suffix == ".csv":
        try:
            reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
            header = next(reader, [])
            if not header or len(header) > 20:
                raise _invalid("表格预览支持 1 至 20 列，请下载原文件或生成精简数据页")
            rows, count = [], 0
            for row in reader:
                if len(row) != len(header) or any(len(cell) > 2000 for cell in row):
                    raise _invalid("CSV 行列不一致或单元格过长，请下载原文件检查")
                count += 1
                if count <= 2000:
                    rows.append({f"c{i}": cell for i, cell in enumerate(row)})
            return {
                "type": "table",
                "columns": [
                    {"key": f"c{i}", "label": cell or f"列 {i + 1}"}
                    for i, cell in enumerate(header)
                ],
                "rows": rows,
                "total_rows": count,
                "preview_limit": 2000,
            }
        except (csv.Error, UnicodeError) as exc:
            raise _invalid("CSV 内容不完整或编码无效") from exc
    if suffix == ".json":
        value = strict_json_loads(content)
        if isinstance(value, dict) and value.get("type") == "page":
            return validate_page(value)
        text = json.dumps(value, ensure_ascii=False, indent=2)
    elif suffix in {".txt", ".md"} or suffix in PRIVATE_TEXT_MATERIAL_TYPES:
        text = content.decode("utf-8-sig")
    else:
        return {"type": "file"}
    return {"type": "text", "content": text[:100000], "truncated": len(text) > 100000}
