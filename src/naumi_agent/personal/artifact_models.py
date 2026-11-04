"""Strict artifact metadata and exact-version reference validation."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from types import MappingProxyType
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.output_publish import ASSET_TYPES, MAX_ASSET_BYTES, validate_asset

ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{32}$")
GRANT_ID = re.compile(r"^asg_[0-9a-f]{32}$")
PRINCIPAL = re.compile(r"^(main|pa_[0-9a-f]{32})$")
REQUEST_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_REFS = 64
MAX_SESSION_REFS = 256
REF_FIELDS = {"artifact_id", "version", "sha256", "grant_id", "grant_revision"}

# Private source documents are data, never public executable browser assets.
PRIVATE_TEXT_MATERIAL_TYPES = MappingProxyType({
    ".py": "text/x-python", ".pyi": "text/x-python",
    ".ts": "text/typescript", ".tsx": "text/plain",
    ".js": "text/plain", ".jsx": "text/plain", ".mjs": "text/plain", ".cjs": "text/plain",
    ".java": "text/plain", ".c": "text/plain", ".h": "text/plain",
    ".cpp": "text/plain", ".cc": "text/plain", ".hpp": "text/plain", ".cs": "text/plain",
    ".go": "text/plain", ".rs": "text/plain", ".rb": "text/plain", ".php": "text/plain",
    ".swift": "text/plain", ".kt": "text/plain", ".kts": "text/plain", ".scala": "text/plain",
    ".sh": "text/x-shellscript", ".bash": "text/x-shellscript", ".zsh": "text/x-shellscript",
    ".ps1": "text/plain", ".psm1": "text/plain", ".psd1": "text/plain", ".sql": "text/plain",
    ".css": "text/plain", ".scss": "text/plain", ".less": "text/plain",
    ".html": "text/plain", ".htm": "text/plain", ".xml": "text/plain",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".toml": "text/plain",
    ".ini": "text/plain", ".cfg": "text/plain", ".conf": "text/plain",
    ".properties": "text/plain", ".env": "text/plain", ".log": "text/plain",
    ".rst": "text/plain", ".tex": "text/plain", ".diff": "text/plain", ".patch": "text/plain",
})


def is_text_artifact(snapshot: dict) -> bool:
    """Recognize validated source documents independently of browser asset policy."""
    mime = snapshot.get("mime_type", "")
    return (
        isinstance(mime, str) and mime.startswith("text/")
        or snapshot.get("suffix") in {".json", ".svg"}
        or snapshot.get("kind") == "material"
        and snapshot.get("suffix") in PRIVATE_TEXT_MATERIAL_TYPES
    )


def invalid(message: str) -> PersonalAgentError:
    return PersonalAgentError("invalid_input", message)


def text(value: Any, label: str, maximum: int, pattern=None) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value)
        or pattern is not None
        and pattern.fullmatch(value) is None
    ):
        raise invalid(f"{label}格式不正确")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise invalid(f"{label}包含无效编码") from exc
    return value


def integer(value: Any, label: str, *, minimum: int = 1, maximum: int = 2**31 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise invalid(f"{label}必须是有效整数")
    return value


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON constant")


def strict_json_loads(value: str | bytes) -> Any:
    """Reject duplicate keys, nonfinite numbers and unbounded structural inputs."""
    try:
        result = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
        return json_value(result, "JSON 内容", maximum=MAX_ASSET_BYTES)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise invalid("JSON 内容包含重复字段、非有限数字或无效格式") from exc


def json_value(value: Any, label: str, *, maximum: int = 65536) -> Any:
    nodes = 0

    def walk(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 100000:
            raise invalid(f"{label}嵌套过深或项目过多")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            item.encode("utf-8")
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in item):
                raise invalid(f"{label}包含无效控制字符")
            return
        if isinstance(item, int) or isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for key, child in item.items():
                walk(key, depth + 1)
                walk(child, depth + 1)
            return
        raise invalid(f"{label}包含不支持的 JSON 值")

    try:
        walk(value)
        encoded = canonical(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise invalid(f"{label}超过保存上限")
        return json.loads(encoded)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise invalid(f"{label}格式不正确") from exc


def json_object(value: Any, label: str, *, maximum: int = 65536) -> dict:
    if not isinstance(value, dict):
        raise invalid(f"{label}必须是 JSON 对象")
    return json_value(value, label, maximum=maximum)


def normalize_refs(value: Any, *, maximum: int = MAX_REFS) -> list[dict]:
    if not isinstance(value, list) or len(value) > maximum:
        raise invalid(f"资料引用最多允许 {maximum} 项")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != REF_FIELDS:
            raise invalid("资料引用需要确切编号、版本、内容摘要和授权版本")
        identity = text(item["artifact_id"], "产物编号", 36, ARTIFACT_ID)
        version = integer(item["version"], "产物版本")
        content_sha = text(item["sha256"], "内容摘要", 64, SHA256)
        grant = item["grant_id"]
        revision = integer(item["grant_revision"], "授权版本", minimum=0)
        if grant == "":
            if revision != 0:
                raise invalid("自有资料的授权版本必须为零")
        else:
            text(grant, "授权编号", 36, GRANT_ID)
            if revision == 0:
                raise invalid("共享资料需要确切授权版本")
        ref = {
            "artifact_id": identity,
            "version": version,
            "sha256": content_sha,
            "grant_id": grant,
            "grant_revision": revision,
        }
        key = canonical(ref)
        if key in seen:
            raise invalid("资料引用包含重复项目")
        seen.add(key)
        result.append(ref)
    return sorted(result, key=canonical)


def validate_content(
    content: Any, suffix: Any, *, kind: str = "output",
) -> tuple[bytes, str, str]:
    if not isinstance(content, bytes):
        raise invalid("文件内容必须是实际字节")
    if not isinstance(suffix, str):
        raise invalid("文件类型格式不正确")
    suffix = ".jpg" if suffix.lower() == ".jpeg" else suffix.lower()
    if kind == "material" and suffix in PRIVATE_TEXT_MATERIAL_TYPES:
        if not content or len(content) > MAX_ASSET_BYTES:
            raise invalid("文件不能为空，且不能超过 20 MB")
        try:
            decoded = content.decode("utf-8-sig", errors="strict")
        except UnicodeError as exc:
            raise invalid("代码、配置和日志文件请使用 UTF-8 编码") from exc
        if "\x00" in decoded:
            raise invalid("代码、配置和日志文件包含二进制空字符，请提供 UTF-8 文本")
        return content, suffix, PRIVATE_TEXT_MATERIAL_TYPES[suffix]
    try:
        validate_asset(content, suffix)
        if suffix == ".json":
            strict_json_loads(content)
        elif suffix == ".pdf":
            validate_pdf_structure(content)
    except ValueError as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise invalid(str(exc)) from exc
    return content, suffix, ASSET_TYPES[suffix]


def validate_pdf_structure(content: bytes) -> None:
    """Validate a bounded PDF catalog/page tree without inflating content streams."""
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise invalid("PDF 已加密，请先导出未加密版本")
        catalog = reader.trailer["/Root"]
        if not isinstance(catalog, DictionaryObject) or catalog.get("/Type") != "/Catalog":
            raise invalid("PDF 缺少有效文档目录")
        seen, pages = set(), 0

        def walk(reference, inherited_box=None, depth=0):
            nonlocal pages
            if depth > 64 or len(seen) >= 5000:
                raise invalid("PDF 页面结构过于复杂，请拆分后上传")
            node = reference.get_object()
            key = (
                (reference.idnum, reference.generation)
                if isinstance(reference, IndirectObject)
                else ("direct", id(node))
            )
            if key in seen or not isinstance(node, DictionaryObject):
                raise invalid("PDF 页面结构包含循环、重复引用或损坏节点")
            seen.add(key)
            box = node.get("/MediaBox", inherited_box)
            if box is not None:
                box = box.get_object()
            kind = node.get("/Type")
            if kind == "/Pages":
                count, kids = node.get("/Count"), node.get("/Kids")
                if count is not None:
                    count = count.get_object()
                if kids is not None:
                    kids = kids.get_object()
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or not 0 <= count <= 1000
                    or not isinstance(kids, ArrayObject)
                    or len(kids) > 5000
                ):
                    raise invalid("PDF 页数超过 1000 页或页面索引无效")
                actual = sum(walk(child, box, depth + 1) for child in kids)
                if actual != count:
                    raise invalid("PDF 页数与实际页面结构不一致")
                return actual
            if kind != "/Page":
                raise invalid("PDF 页面类型无效")
            pages += 1
            if pages > 1000:
                raise invalid("PDF 最多支持 1000 页，请拆分后上传")
            if (
                not isinstance(box, ArrayObject)
                or len(box) != 4
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in box
                )
                or box[2] <= box[0]
                or box[3] <= box[1]
            ):
                raise invalid("PDF 页面尺寸无效")
            return 1

        total = walk(catalog["/Pages"])
        if total == 0:
            raise invalid("PDF 没有可用页面，请导出至少一页")
    except (
        PyPdfError,
        KeyError,
        AttributeError,
        AssertionError,
        IndexError,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise invalid("PDF 文档结构损坏，请重新导出后上传") from exc


def normalize_recipient_scope(value: Any, recipient_id: str) -> dict:
    scope = json_object(value, "接收范围", maximum=1024 * 1024)
    if scope == {}:
        return {}
    if set(scope) != {"kind", "team_id", "team_version", "member_ids", "teams"}:
        raise invalid("团队分享范围需要确切团队、版本、叶子成员与祖先关系")
    if scope["kind"] != "team" or scope["team_id"] != recipient_id:
        raise invalid("团队分享范围与接收团队不一致")
    team_id = text(scope["team_id"], "接收团队", 35, PRINCIPAL)
    if team_id == "main":
        raise invalid("主 Agent不是成员团队")
    revision = integer(scope["team_version"], "团队版本")
    members, teams = scope["member_ids"], scope["teams"]
    if not isinstance(members, list) or len(members) > 128:
        raise invalid("团队分享最多支持 128 位叶子成员")
    if not isinstance(teams, list) or not 1 <= len(teams) <= 128:
        raise invalid("团队分享需要 1 至 128 个祖先团队")
    for member in members:
        text(member, "团队叶子成员", 35, PRINCIPAL)
        if member == "main":
            raise invalid("团队叶子成员必须是已有 Agent")
    if len(set(members)) != len(members):
        raise invalid("团队分享包含重复叶子成员")
    graph = {}
    for item in teams:
        if not isinstance(item, dict) or set(item) != {"agent_id", "agent_version", "member_ids"}:
            raise invalid("团队祖先关系字段不完整")
        identity = text(item["agent_id"], "祖先团队", 35, PRINCIPAL)
        if identity == "main" or identity in graph:
            raise invalid("团队祖先身份重复或无效")
        version = integer(item["agent_version"], "祖先团队版本")
        children = item["member_ids"]
        if not isinstance(children, list) or len(children) > 128:
            raise invalid("单个团队最多支持 128 个直接成员")
        for child in children:
            text(child, "团队直接成员", 35, PRINCIPAL)
            if child == "main":
                raise invalid("团队直接成员必须是已有 Agent 或团队")
        if len(set(children)) != len(children):
            raise invalid("团队祖先关系包含重复成员")
        graph[identity] = {
            "agent_id": identity,
            "agent_version": version,
            "member_ids": sorted(children),
        }
    if team_id not in graph or graph[team_id]["agent_version"] != revision:
        raise invalid("接收团队与祖先快照版本不一致")
    leaves, visited, visiting = set(), set(), set()

    def walk(identity, depth=0):
        if identity not in graph:
            leaves.add(identity)
            return
        if identity in visiting or depth > 8:
            raise invalid("团队分享关系存在循环或嵌套超过 8 层")
        if identity in visited:
            return
        visiting.add(identity)
        for child in graph[identity]["member_ids"]:
            walk(child, depth + 1)
        visiting.remove(identity)
        visited.add(identity)

    walk(team_id)
    if visited != set(graph) or leaves != set(members) or set(members) & set(graph):
        raise invalid("团队分享的祖先关系与实际叶子集合不一致")
    return {
        "kind": "team",
        "team_id": team_id,
        "team_version": revision,
        "member_ids": sorted(members),
        "teams": sorted(graph.values(), key=lambda item: item["agent_id"]),
    }
