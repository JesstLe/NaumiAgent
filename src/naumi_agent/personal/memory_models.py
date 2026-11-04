"""Strict trusted-memory records, exact references and deterministic lexical tokens."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime

from naumi_agent.personal.artifact_models import json_object, normalize_refs, strict_json_loads
from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError

OWNER = re.compile(r"^[0-9a-f]{64}$")
SHA256 = OWNER
ACTOR = re.compile(r"^(main|pa_[0-9a-f]{32})$")
MEMORY_ID = re.compile(r"^pm_[0-9a-f]{32}$")
GRANT_ID = re.compile(r"^pmg_[0-9a-f]{32}$")
ITEM_ID = re.compile(r"^pi_[0-9a-f]{32}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
INDEX_ID = re.compile(r"^pmidx_[0-9a-f]{32}$")
STATES = frozenset({"proposed", "active", "superseded", "rejected", "deleted"})
CATEGORIES = frozenset({"preference", "fact", "decision", "experience", "plan_template"})
SOURCE_KINDS = frozenset({"manual", "message", "artifact", "legacy", "compaction"})
AUTHORITIES = frozenset({"user", "assistant", "external", "unknown"})
HUMAN_CHANNELS = frozenset({"management_web", "manual_cli"})
TOKENIZER_VERSION = "cjk-bigram-word-v1"
REF_FIELDS = frozenset(
    {
        "memory_id",
        "revision",
        "content_sha256",
        "grant_id",
        "grant_revision",
    }
)


def invalid(message: str) -> PersonalAgentError:
    return PersonalAgentError("invalid_input", message)


def text(value, label, maximum=8000, *, empty=False, pattern=None):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not empty
        and not value.strip()
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


def integer(value, label, minimum=1, maximum=2**31 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise invalid(f"{label}必须是有效整数")
    return value


def choice(value, values, label):
    if not isinstance(value, str) or value not in values:
        raise invalid(f"{label}选项不正确")
    return value


def object_value(value, label, *, maximum=1024 * 1024):
    if isinstance(value, (str, bytes)):
        value = strict_json_loads(value)
    return json_object(value, label, maximum=maximum)


def exact_keys(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise invalid(f"{label}包含未知字段或缺少必要内容")
    return value


def timestamp(value, label="记忆时间"):
    raw = text(value, label, 80)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise invalid(f"{label}需要带时区的时间") from exc


def content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def topic_key(value):
    value = text(value, "记忆主题", 120)
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise invalid("记忆主题包含不可见字符")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return text(normalized, "记忆主题", 360)


def normalize_scope(value):
    v = exact_keys(
        object_value(value, "记忆范围"), {"kind", "workspace_key", "item_id"}, "记忆范围"
    )
    kind = choice(v["kind"], {"personal", "workspace", "item"}, "记忆范围")
    workspace, item = v["workspace_key"], v["item_id"]
    text(workspace, "工作区标识", 64, empty=True)
    text(item, "事项标识", 35, empty=True)
    if kind == "personal":
        if workspace or item:
            raise invalid("个人记忆不绑定工作区或事项")
    elif SHA256.fullmatch(workspace) is None or (
        kind == "workspace" and item or kind == "item" and ITEM_ID.fullmatch(item) is None
    ):
        raise invalid("记忆范围与工作区、事项不一致")
    return {"kind": kind, "workspace_key": workspace, "item_id": item}


def within_ceiling(scope, ceiling):
    target, source = normalize_scope(scope), normalize_scope(ceiling)
    if source["kind"] == "personal":
        return True
    if target["workspace_key"] != source["workspace_key"]:
        return False
    if source["kind"] == "workspace":
        return target["kind"] in {"workspace", "item"}
    return target == source


def normalize_source(value):
    fields = {
        "kind",
        "authority",
        "locator",
        "excerpt",
        "excerpt_sha256",
        "artifact_refs",
        "scope_ceiling",
        "captured_at",
    }
    v = exact_keys(object_value(value, "记忆来源", maximum=128 * 1024), fields, "记忆来源")
    kind = choice(v["kind"], SOURCE_KINDS, "来源类型")
    authority = choice(v["authority"], AUTHORITIES, "来源作者")
    excerpt = text(v["excerpt"], "来源摘录", 8000, empty=True)
    checksum = text(v["excerpt_sha256"], "来源摘要", 64, pattern=SHA256)
    if content_digest(excerpt) != checksum:
        raise invalid("来源摘录与摘要不一致")
    locator = object_value(v["locator"], "来源定位", maximum=8192)

    def check_locator(node):
        if isinstance(node, dict):
            if set(node) & {
                "content",
                "excerpt",
                "quote",
                "text",
                "body",
                "prompt",
                "instructions",
            }:
                raise invalid("来源定位只保存对象身份，不保存正文")
            for child in node.values():
                check_locator(child)
        elif isinstance(node, list):
            if len(node) > 32:
                raise invalid("来源定位对象过多")
            for child in node:
                check_locator(child)
        elif isinstance(node, str):
            text(node, "来源对象身份", 256, empty=True)

    check_locator(locator)
    return {
        "kind": kind,
        "authority": authority,
        "locator": locator,
        "excerpt": excerpt,
        "excerpt_sha256": checksum,
        "artifact_refs": normalize_refs(v["artifact_refs"]),
        "scope_ceiling": normalize_scope(v["scope_ceiling"]),
        "captured_at": timestamp(v["captured_at"], "来源采集时间"),
    }


def normalize_human(value):
    if value is None:
        return None
    v = object_value(value, "人工确认", maximum=8192)
    allowed = {
        "channel",
        "action",
        "actor_kind",
        "request_id",
        "confirmed_at",
        "receipt_id",
        "source_sha256",
        "previous_source_sha256",
    }
    if "channel" not in v or set(v) - allowed:
        raise invalid("人工确认包含未知字段或缺少可信渠道")
    choice(v["channel"], HUMAN_CHANNELS, "人工确认渠道")
    if "actor_kind" in v and v["actor_kind"] != "human":
        raise invalid("确认主体必须是人工操作")
    for key in {"action", "receipt_id"} & v.keys():
        text(v[key], "人工确认标识", 160)
    if "request_id" in v:
        text(v["request_id"], "请求编号", 128, pattern=REQUEST_ID)
    if "confirmed_at" in v:
        v["confirmed_at"] = timestamp(v["confirmed_at"], "人工确认时间")
    for key in {"source_sha256", "previous_source_sha256"} & v.keys():
        text(v[key], "确认来源摘要", 64, pattern=SHA256)
    return v


def human_semantics(value):
    v = normalize_human(value)
    if v is None:
        return None
    return {k: x for k, x in v.items() if k not in {"confirmed_at", "receipt_id"}}


def source_semantics(value):
    return {k: x for k, x in normalize_source(value).items() if k != "captured_at"}


def normalize_ref(value):
    v = exact_keys(object_value(value, "记忆引用"), REF_FIELDS, "记忆引用")
    memory = text(v["memory_id"], "记忆编号", 35, pattern=MEMORY_ID)
    revision = integer(v["revision"], "记忆修订")
    checksum = text(v["content_sha256"], "记忆摘要", 64, pattern=SHA256)
    grant = text(v["grant_id"], "记忆分享编号", 36, empty=True)
    grant_revision = integer(v["grant_revision"], "分享版本", minimum=0)
    if bool(grant) != bool(grant_revision) or grant and GRANT_ID.fullmatch(grant) is None:
        raise invalid("记忆引用与分享版本不一致")
    return {
        "memory_id": memory,
        "revision": revision,
        "content_sha256": checksum,
        "grant_id": grant,
        "grant_revision": grant_revision,
    }


def reference(record, grant=None):
    return {
        "memory_id": record["memory_id"],
        "revision": record["revision"],
        "content_sha256": record["content_sha256"],
        "grant_id": grant["grant_id"] if grant else "",
        "grant_revision": grant["revision"] if grant else 0,
    }


def memory_id_for_request(owner_key, request_id):
    text(owner_key, "记忆所有者标识", 64, pattern=OWNER)
    text(request_id, "请求编号", 128, pattern=REQUEST_ID)
    return "pm_" + digest({"owner_key": owner_key, "request_id": request_id})[:32]


def request_fingerprint(actor_id, workspace_key, operation, payload):
    return digest(
        {
            "actor_id": actor_id,
            "workspace_key": workspace_key,
            "operation": operation,
            "payload": payload,
        }
    )


def _cjk(char):
    value = ord(char)
    return 0x3400 <= value <= 0x9FFF or 0x20000 <= value <= 0x3134F or 0xF900 <= value <= 0xFAFF


def tokenize(value):
    """Versioned language-aware tokens; no caller text becomes FTS syntax."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = []
    for word in re.findall(r"[^\W_]+", normalized, re.UNICODE):
        latin, cjk = [], []

        def flush_latin():
            if latin:
                tokens.append("w" + "".join(latin))
                latin.clear()

        def flush_cjk():
            if cjk:
                tokens.extend("c" + c.encode("utf-8").hex() for c in cjk)
                tokens.extend(
                    "b" + "".join(cjk[i : i + 2]).encode("utf-8").hex() for i in range(len(cjk) - 1)
                )
                cjk.clear()

        for char in word:
            if _cjk(char):
                flush_latin()
                cjk.append(char)
            else:
                flush_cjk()
                latin.append(char)
        flush_latin()
        flush_cjk()
    return list(dict.fromkeys(tokens))


def lexical_query(value):
    value = text(value, "记忆查询", 2000, empty=True)
    tokens = tokenize(value)[:256]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


__all__ = [
    "ACTOR",
    "AUTHORITIES",
    "CATEGORIES",
    "GRANT_ID",
    "HUMAN_CHANNELS",
    "INDEX_ID",
    "ITEM_ID",
    "MEMORY_ID",
    "OWNER",
    "REF_FIELDS",
    "REQUEST_ID",
    "SHA256",
    "SOURCE_KINDS",
    "STATES",
    "TOKENIZER_VERSION",
    "canonical",
    "choice",
    "content_digest",
    "digest",
    "human_semantics",
    "integer",
    "invalid",
    "lexical_query",
    "memory_id_for_request",
    "normalize_human",
    "normalize_ref",
    "normalize_scope",
    "normalize_source",
    "object_value",
    "reference",
    "request_fingerprint",
    "source_semantics",
    "text",
    "timestamp",
    "tokenize",
    "topic_key",
    "within_ceiling",
]
