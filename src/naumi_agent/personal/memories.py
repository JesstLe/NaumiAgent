"""Trusted memory admission, live source authorization and scoped retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.personal.artifact_models import is_text_artifact, normalize_refs
from naumi_agent.personal.memory_index import PersonalMemoryIndex, revision_key
from naumi_agent.personal.memory_models import (
    ACTOR,
    CATEGORIES,
    REQUEST_ID,
    SHA256,
    canonical,
    content_digest,
    digest,
    integer,
    normalize_ref,
    normalize_scope,
    normalize_source,
    object_value,
    reference,
    request_fingerprint,
    text,
)
from naumi_agent.personal.memory_store import PersonalMemoryStore
from naumi_agent.personal.memory_tools import HUMAN_OPERATIONS, validate_operation_payload
from naumi_agent.personal.models import PersonalAgentError

logger = logging.getLogger(__name__)
_CHANNELS = {
    "web": "management_web",
    "cli": "manual_cli",
    "tui": "manual_cli",
    "management_web": "management_web",
    "manual_cli": "manual_cli",
}
_READS = frozenset({"list", "get", "search", "history"})


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _unique_refs(refs):
    return normalize_refs(list({canonical(r): r for r in refs}.values()))


class PersonalMemoryService:
    """All human, tool, compatibility and runtime paths share this authority."""

    def __init__(self, engine, *, store=None, index=None):
        self.engine, self.personal = engine, engine.personal_agents
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode("utf-8"),
        ).hexdigest()
        self.workspace_key = self.personal.workspace_key
        self.store = store or PersonalMemoryStore(
            self.personal.store.base_dir / "memory",
            owner_key=self.owner_key,
        )
        self.index = index or PersonalMemoryIndex(self.store.base_dir / "semantic", self.owner_key)
        session_db = Path(engine.config.memory.session_db_path).resolve()
        self.runtime_boundary_marker = session_db.with_name(
            f".personal-memory-{self.workspace_key}-v1",
        )
        self.owner_boundary_marker = self.personal.store.base_dir.parent / (
            f".personal-memory-owner-{self.owner_key}-v1"
        )
        self._identity = None
        self._sync_lock, self._mutation_lock = asyncio.Lock(), asyncio.Lock()
        self._worker = None
        self._wake = asyncio.Event()

    @property
    def enabled(self):
        return bool(
            self._identity
            or self.store.db_path.exists()
            or self.runtime_boundary_marker.exists()
            or self.owner_boundary_marker.exists()
            or Path(self.engine.config.memory.vector_db_path).exists()
        )

    def _guard(self):
        identity = self.store.identity(owner_key=self.owner_key)
        expected = {"version": 1, "owner_key": self.owner_key, "instance_id": identity}
        try:
            if self._identity and self._identity != identity:
                raise ValueError("memory instance changed")
            for marker in (self.owner_boundary_marker, self.runtime_boundary_marker):
                if marker.exists() or marker.is_symlink():
                    if (
                        marker.is_symlink()
                        or not identity
                        or json.loads(
                            marker.read_text("utf-8"),
                        )
                        != expected
                    ):
                        raise ValueError("memory witness mismatch")
                elif identity:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with marker.open("xb") as stream:
                            stream.write((canonical(expected) + "\n").encode("utf-8"))
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        if marker.is_symlink() or json.loads(marker.read_text("utf-8")) != expected:
                            raise ValueError("memory witness changed") from None
            if identity:
                self._identity = identity
        except (OSError, ValueError) as exc:
            raise PersonalAgentError(
                "storage_error", "记忆资料库与原凭据不一致，请恢复原资料库"
            ) from exc

    async def _call(self, method, **kwargs):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            getattr(self.store, method),
            owner_key=self.owner_key,
            **kwargs,
        )
        await asyncio.to_thread(self._guard)
        return result

    def _actor(self, actor_id, *, caller_actor=None, write=False):
        if not isinstance(actor_id, str) or ACTOR.fullmatch(actor_id) is None:
            raise PersonalAgentError("invalid_input", "请选择真实的 Agent")
        if caller_actor is not None and caller_actor != actor_id:
            raise PersonalAgentError("not_found", "该记忆不属于当前 Agent")
        if actor_id != "main":
            profile = self.personal.get(actor_id)
            if write and profile.status != "active":
                raise PersonalAgentError("agent_inactive", "该 Agent 已暂停或归档")
        return actor_id

    def _version(self, actor_id):
        return 0 if actor_id == "main" else self.personal.get(actor_id).version

    def _execution_context(self, actor_id):
        session = getattr(self.engine, "_session", None)
        context = getattr(self.engine, "_personal_memory_execution_context", None)
        child = bool(
            context is not None
            or getattr(self.engine, "_isolated_child", False)
            or getattr(session, "personal_execution_id", "")
            or getattr(self.engine, "_personal_execution_authorizer", None)
        )
        if not child:
            return None
        required = {"actor_id", "origin_actor_id", "workspace_key", "item_id", "artifact_refs"}
        if (
            not isinstance(context, dict)
            or not required <= context.keys()
            or context.keys() - required - {"origin_actor_ids"}
            or getattr(self.engine, "_personal_memory_parent_enabled", False) is not True
            or getattr(getattr(self.engine, "personal_memory_runtime", None), "enabled", False)
            is not True
            or context["actor_id"] != actor_id
            or context["workspace_key"] != self.workspace_key
            or session is None
        ):
            raise PersonalAgentError("scope_denied", "本次执行缺少有效的记忆范围绑定")
        self._actor(actor_id, write=True)
        profile = self.personal.current(session.id)
        actual = profile.id if profile else "main"
        marker = getattr(session, "personal_agent_id", "")
        if actual != actor_id or marker and marker != actor_id:
            raise PersonalAgentError("binding_conflict", "记忆范围与实际执行 Agent 不一致")
        self.personal._session_valid(session)
        item = context["item_id"]
        if item is not None:
            normalize_scope({"kind": "item", "workspace_key": self.workspace_key, "item_id": item})
        origins = context.get("origin_actor_ids", [])
        if not isinstance(origins, list) or len(origins) > 64:
            raise PersonalAgentError("scope_denied", "记忆回复路径缺少有效的来源身份")
        origins = [context["origin_actor_id"], *origins]
        for origin in origins:
            self._actor(origin)
        return {
            **context,
            "origin_actor_ids": list(dict.fromkeys(origins)),
            "artifact_refs": normalize_refs(context["artifact_refs"]),
        }

    def _management_allowed(self, actor_id):
        if self._execution_context(actor_id) is not None:
            raise PersonalAgentError("scope_denied", "请从人工记忆面板或日常对话管理记忆")

    def _effective_item(self, actor_id, item_id):
        context = self._execution_context(actor_id)
        if context is None:
            return item_id
        if item_id is not None and item_id != context["item_id"]:
            raise PersonalAgentError("scope_denied", "该事项不属于本次执行上下文")
        return context["item_id"]

    async def _execution_allows(self, record, actor_id, grant=None):
        context = self._execution_context(actor_id)
        if context is None:
            return True
        if record["state"] != "active" or not self._scope_matches(record):
            return False
        if record["scope"]["kind"] == "item" and (record["scope"]["item_id"] != context["item_id"]):
            return False
        try:
            # Every reply-path participant must authorize this exact current head.
            await self._call("authorize_ref", actor_id=actor_id, ref=reference(record, grant))
            await self._source_authorized(record, actor_id, grant)
            frozen = {canonical(ref) for ref in context["artifact_refs"]}
            source_refs = grant["source_refs"] if grant else record["source"]["artifact_refs"]
            if any(canonical(ref) not in frozen for ref in source_refs):
                return False
            for origin in context["origin_actor_ids"]:
                if origin == actor_id:
                    continue
                original_grant = await self._access(record, origin)
                await self._call(
                    "authorize_ref", actor_id=origin, ref=reference(record, original_grant)
                )
                await self._source_authorized(record, origin, original_grant)
        except PersonalAgentError as exc:
            if exc.code == "storage_error":
                raise
            return False
        return True

    async def _scope(self, kind, item_id=None, *, actor_id=None):
        if kind not in {"personal", "workspace", "item"}:
            raise PersonalAgentError("invalid_input", "请选择个人、工作区或事项范围")
        if kind == "item":
            item = await self.engine.personal_items._item(item_id)
            if actor_id and actor_id not in {
                item["definition"]["assignee"]["id"],
                item["definition"]["origin"]["actor_id"],
            }:
                raise PersonalAgentError("scope_denied", "该事项不属于当前 Agent 的上下文")
        elif item_id:
            raise PersonalAgentError("invalid_input", "只有事项记忆需要事项编号")
        return normalize_scope(
            {
                "kind": kind,
                "workspace_key": "" if kind == "personal" else self.workspace_key,
                "item_id": item_id if kind == "item" else "",
            }
        )

    def _scope_matches(self, record, kind="all", item_id=None):
        scope = record["scope"]
        if scope["kind"] != "personal" and scope["workspace_key"] != self.workspace_key:
            return False
        if kind != "all" and scope["kind"] != kind:
            return False
        return not item_id or scope["kind"] != "item" or scope["item_id"] == item_id

    async def _records(self, *, actor_id=None, state=None):
        result, offset = [], 0
        while True:
            page = await self._call(
                "list", actor_id=actor_id, state=state, limit=100, offset=offset
            )
            result.extend(page)
            if len(page) < 100:
                return result
            offset += 100

    async def _source_authorized(self, record, actor_id, grant=None, *, _visited=frozenset()):
        source = record.get("source")
        if source is None:
            return
        key = (
            record["memory_id"],
            record["revision"],
            actor_id,
            grant.get("grant_id", "pending-share") if grant else "",
        )
        if key in _visited or len(_visited) >= 16:
            raise PersonalAgentError("source_unavailable", "记忆来源依赖出现循环或超过深度上限")
        visited = _visited | {key}
        refs = source["artifact_refs"]
        if refs:
            await self.engine.personal_artifacts.authorize_refs(
                actor_id=record["actor_id"], refs=refs
            )
        if grant is not None:
            if grant["recipient_version"] != self._version(actor_id):
                raise PersonalAgentError("memory_access_revoked", "原记忆分享的 Agent 配置已变化")
            if grant.get("source_refs"):
                await self.engine.personal_artifacts.authorize_refs(
                    actor_id=actor_id, refs=grant["source_refs"]
                )
        execution = getattr(self.engine, "_personal_memory_execution_context", None)
        if isinstance(execution, dict) and execution.get("actor_id") == actor_id:
            execution = self._execution_context(actor_id)
            frozen = {canonical(ref) for ref in execution["artifact_refs"]}
            actual_refs = grant["source_refs"] if grant else refs
            if any(canonical(ref) not in frozen for ref in actual_refs):
                raise PersonalAgentError("memory_access_revoked", "来源资料未包含在本次冻结输入中")
        dependencies = source["locator"].get("memory_refs", [])
        if not isinstance(dependencies, list) or len(dependencies) > 32:
            raise PersonalAgentError("source_unavailable", "记忆依赖数量或格式不正确")
        if not dependencies:
            return
        source_actor = self._actor(source["locator"].get("source_actor_id"))
        for ref in dependencies:
            ref = normalize_ref(ref)
            original = await self._call("authorize_ref", actor_id=source_actor, ref=ref)
            original_grant = await self._access(
                original, source_actor, exact_ref=ref, _visited=visited
            )
            if original_grant is None:
                await self._source_authorized(original, source_actor, _visited=visited)
            if actor_id != source_actor:
                recipient_grant = await self._access(original, actor_id, _visited=visited)
                await self._call(
                    "authorize_ref", actor_id=actor_id, ref=reference(original, recipient_grant)
                )
                if recipient_grant is None:
                    await self._source_authorized(original, actor_id, _visited=visited)

    async def _access(self, record, actor_id, *, exact_ref=None, _visited=frozenset()):
        if not self._scope_matches(record):
            raise PersonalAgentError("not_found", "该记忆不在当前工作区范围")
        if record["actor_id"] == actor_id:
            if exact_ref is not None and exact_ref.get("grant_id"):
                raise PersonalAgentError("memory_access_revoked", "该记忆引用与原所属 Agent 不一致")
            return None
        grants = await self._call("grants", memory_id=record["memory_id"])
        for grant in grants:
            if (
                grant["recipient_actor_id"] == actor_id
                and grant["state"] == "active"
                and grant["memory_revision"] == record["revision"]
                and grant["content_sha256"] == record["content_sha256"]
                and (
                    exact_ref is None
                    or grant["grant_id"] == exact_ref["grant_id"]
                    and grant["revision"] == exact_ref["grant_revision"]
                )
            ):
                try:
                    await self._source_authorized(record, actor_id, grant, _visited=_visited)
                except PersonalAgentError as exc:
                    if exc.code == "storage_error" or exact_ref is not None:
                        raise
                    continue
                return grant
        raise PersonalAgentError("not_found", "该记忆没有向当前 Agent 分享")

    async def _active_ref(self, record):
        for other in await self._records(actor_id=record["actor_id"], state="active"):
            if other["scope"] == record["scope"] and other["topic_key"] == record["topic_key"]:
                return reference(other)
        return None

    async def _dto(self, record, actor_id, *, grant=None):
        value = {
            **record,
            "access": "shared" if grant else "owned",
            "ref": reference(record, grant),
            "source_available": True,
            "error": "",
            "source_mode": "captured_snapshot",
            "locator_live": await self._locator_status(record),
        }
        value["active_ref"] = await self._active_ref(record) if not grant else None
        if record["state"] in {"deleted", "rejected", "superseded"}:
            # The internal Store uses an empty payload; the public DTO makes
            # absence explicit, including idempotency/request replay results.
            value.update(content=None, source=None, source_available=False)
            return value
        try:
            await self._source_authorized(record, actor_id, grant)
        except PersonalAgentError as exc:
            if exc.code == "storage_error":
                raise
            value.update(
                content=None,
                source=None,
                topic="来源已失效",
                source_available=False,
                error="原记忆来源或分享授权已失效",
            )
        return value

    async def _locator_status(self, record):
        source = record.get("source")
        if not source or source["kind"] not in {"message", "compaction"}:
            return "not_applicable"
        locator = source["locator"]
        if "message_index" not in locator:
            return "captured_only"
        session = await self.engine.session_store.load(locator.get("session_id", ""))
        if session is None:
            return "missing"
        history = session.messages
        current = getattr(self.engine, "_session", None)
        if (
            current is not None
            and current.id == session.id
            and getattr(self.engine, "_personal_active_turn", None)
        ):
            history = getattr(self.engine, "_full_history", history)
        index = locator["message_index"]
        if type(index) is not int or not 0 <= index < len(history):
            return "missing"
        message = history[index]
        raw = message.get("content")
        if (
            not isinstance(raw, str)
            or content_digest(raw) != locator["message_sha256"]
            or message.get("role", "unknown") != locator["role"]
            or locator.get("message_id")
            and message.get("id", "") != locator["message_id"]
        ):
            return "changed"
        return "live"

    async def get(self, memory_id, actor_id="main", caller_actor=None):
        self._actor(actor_id, caller_actor=caller_actor)
        self._execution_context(actor_id)
        record = await self._call("get", memory_id=memory_id)
        grant = await self._access(record, actor_id)
        if not await self._execution_allows(record, actor_id, grant):
            raise PersonalAgentError("not_found", "该记忆不在本次执行及回复路径的共同范围")
        return await self._dto(record, actor_id, grant=grant)

    async def list(
        self,
        actor_id="main",
        scope="all",
        item_id=None,
        state=None,
        query="",
        limit=50,
        offset=0,
        caller_actor=None,
    ):
        self._actor(actor_id, caller_actor=caller_actor)
        item_id = self._effective_item(actor_id, item_id)
        validate_operation_payload(
            "list",
            {
                "scope": scope,
                "query": query,
                "limit": limit,
                "offset": offset,
                **({"item_id": item_id} if item_id is not None else {}),
                **({"state": state} if state is not None else {}),
            },
        )
        integer(limit, "每页记忆数", maximum=100)
        integer(offset, "记忆位置", minimum=0)
        text(query, "记忆查询", 2000, empty=True)
        if scope not in {"all", "personal", "workspace", "item"}:
            raise PersonalAgentError("invalid_input", "记忆范围无效")
        if scope == "item":
            await self._scope(scope, item_id, actor_id=actor_id)
        if state not in {
            None,
            "active",
            "proposed",
            "superseded",
            "rejected",
            "deleted",
            "conflict",
        }:
            raise PersonalAgentError("invalid_input", "记忆状态无效")
        records = await self._records(actor_id=actor_id)
        existing = {r["memory_id"] for r in records}
        for grant in await self._call("visible_grants", recipient_actor_id=actor_id):
            if grant["memory_id"] not in existing:
                records.append(await self._call("get", memory_id=grant["memory_id"]))
                existing.add(grant["memory_id"])
        values = []
        for record in records:
            if not self._scope_matches(record, scope, item_id):
                continue
            if state not in {None, "conflict"} and record["state"] != state:
                continue
            try:
                grant = await self._access(record, actor_id)
            except PersonalAgentError as exc:
                if exc.code not in {"not_found", "memory_access_revoked"}:
                    raise
                continue
            if not await self._execution_allows(record, actor_id, grant):
                continue
            dto = await self._dto(record, actor_id, grant=grant)
            if state == "conflict" and not (
                dto["state"] == "proposed" and dto.get("active_ref") is not None
            ):
                continue
            if (
                query
                and query.casefold()
                not in (str(dto.get("topic", "")) + " " + str(dto.get("content") or "")).casefold()
            ):
                continue
            values.append(dto)
        values.sort(key=lambda r: (r["updated_at"], r["memory_id"]), reverse=True)
        return {
            "memories": values[offset : offset + limit],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if len(values) > offset + limit else None,
        }

    async def history(self, memory_id, actor_id="main", caller_actor=None):
        dto = await self.get(memory_id, actor_id, caller_actor)
        if not dto["source_available"]:
            return {"history": [], "source_available": False, "error": dto["error"]}
        return {"history": await self._call("history", memory_id=memory_id)}

    async def grants(self, memory_id, actor_id="main", caller_actor=None):
        dto = await self.get(memory_id, actor_id, caller_actor)
        if dto["actor_id"] != actor_id:
            raise PersonalAgentError("scope_denied", "只有记忆所属 Agent 可以管理分享")
        return {"grants": await self._call("grants", memory_id=memory_id)}

    async def _eligible(self, actor_id, *, scope="all", item_id=None, category=None):
        self._actor(actor_id)
        item_id = self._effective_item(actor_id, item_id)
        records = await self._records(actor_id=actor_id, state="active")
        present = {record["memory_id"] for record in records}
        for grant in await self._call("visible_grants", recipient_actor_id=actor_id):
            if grant["memory_id"] not in present:
                records.append(await self._call("get", memory_id=grant["memory_id"]))
                present.add(grant["memory_id"])
        authorized, grants = [], {}
        for record in records:
            if (
                record["state"] != "active"
                or not self._scope_matches(record, scope, item_id)
                or record["scope"]["kind"] == "item"
                and record["scope"]["item_id"] != item_id
                or category is not None
                and record["category"] != category
            ):
                continue
            try:
                grant = await self._access(record, actor_id)
                await self._source_authorized(record, actor_id, grant)
                if not await self._execution_allows(record, actor_id, grant):
                    continue
            except PersonalAgentError as exc:
                if exc.code == "storage_error":
                    raise
                continue
            authorized.append(record)
            if grant:
                grants[record["memory_id"]] = grant
        return authorized, grants

    @staticmethod
    def _winners(records, actor_id):
        priority = {"personal": 0, "workspace": 1, "item": 2}
        winners, groups = {}, {}
        for record in records:
            groups.setdefault(record["topic_key"], []).append(record)
            score = (
                priority[record["scope"]["kind"]],
                int(record["actor_id"] == actor_id),
                record["updated_at"],
                record["memory_id"],
            )
            old = winners.get(record["topic_key"])
            if old is None or score > old[0]:
                winners[record["topic_key"]] = (score, record)
        return [value[1] for value in winners.values()], groups

    async def resolve_ref(self, ref, actor_id, *, item_id=None):
        self._actor(actor_id)
        item_id = self._effective_item(actor_id, item_id)
        ref = normalize_ref(ref)
        record = await self._call("authorize_ref", actor_id=actor_id, ref=ref)
        if not self._scope_matches(record) or (
            record["scope"]["kind"] == "item" and record["scope"]["item_id"] != item_id
        ):
            raise PersonalAgentError("memory_access_revoked", "原记忆不在本次对话范围")
        grant = await self._access(record, actor_id, exact_ref=ref)
        await self._source_authorized(record, actor_id, grant)
        if not await self._execution_allows(record, actor_id, grant):
            raise PersonalAgentError(
                "memory_access_revoked", "原记忆不在本次执行及回复路径的共同范围"
            )
        return record, grant

    async def search(
        self,
        query,
        actor_id="main",
        scope="all",
        item_id=None,
        limit=10,
        caller_actor=None,
        category=None,
    ):
        self._actor(actor_id, caller_actor=caller_actor)
        item_id = self._effective_item(actor_id, item_id)
        validate_operation_payload(
            "search",
            {
                "query": query,
                "scope": scope,
                "limit": limit,
                **({"item_id": item_id} if item_id is not None else {}),
                **({"category": category} if category is not None else {}),
            },
        )
        text(query, "记忆查询", 2000)
        integer(limit, "召回数量", maximum=20)
        if category is not None and category not in CATEGORIES:
            raise PersonalAgentError("invalid_input", "记忆分类无效")
        if scope not in {"all", "personal", "workspace", "item"}:
            raise PersonalAgentError("invalid_input", "记忆范围无效")
        if scope == "item":
            await self._scope(scope, item_id, actor_id=actor_id)
        records, grants = await self._eligible(
            actor_id, scope=scope, item_id=item_id, category=category
        )
        candidates_before_priority = len(records)
        records, topic_groups = self._winners(records, actor_id)
        await self.sync_index()
        semantic, error = [], ""
        try:
            semantic = await asyncio.to_thread(
                self.index.query, query, records, limit=max(limit * 3, 20)
            )
        except PersonalAgentError as exc:
            error = exc.message
        lexical = (
            await self._call(
                "lexical_search",
                query=query,
                memory_ids=[r["memory_id"] for r in records],
                limit=max(limit * 3, 20),
            )
            if records
            else []
        )
        candidates = {r["memory_id"]: r for r in records}
        scores, similarities = {}, {}
        for i, value in enumerate(lexical):
            identity = value["memory_id"]
            if identity in candidates:
                scores[identity] = scores.get(identity, 0) + 1 / (60 + i + 1)
        for i, value in enumerate(semantic):
            record = candidates.get(value["memory_id"])
            if record is not None and revision_key(record) == value["ref_key"]:
                scores[record["memory_id"]] = scores.get(record["memory_id"], 0) + 1 / (60 + i + 1)
                similarities[record["memory_id"]] = value["similarity"]
        result = []
        for identity in sorted(scores, key=lambda m: (-scores[m], m)):
            record = candidates[identity]
            grant = grants.get(identity)
            ref = reference(record, grant)
            try:
                current, current_grant = await self.resolve_ref(ref, actor_id, item_id=item_id)
            except PersonalAgentError as exc:
                if exc.code == "storage_error":
                    raise
                continue
            dto = await self._dto(current, actor_id, grant=current_grant)
            result.append(
                {
                    **dto,
                    "conflicts": [
                        reference(other, grants.get(other["memory_id"]))
                        for other in topic_groups[current["topic_key"]]
                        if other["memory_id"] != current["memory_id"]
                    ],
                    "retrieval": {
                        "score": scores[identity],
                        "semantic_similarity": similarities.get(identity),
                    },
                }
            )
            if len(result) == limit:
                break
        return {
            "memories": result,
            "diagnostics": {
                "authorized_candidates": len(records),
                "omitted_count": candidates_before_priority - len(records),
                "conflict_count": sum(len(group) > 1 for group in topic_groups.values()),
                "semantic_used": bool(semantic),
                "semantic_error": error,
                "lexical_hits": len(lexical),
                **self.index.status(),
            },
        }

    async def _capture_source(self, payload, actor_id, scope, *, manual=False, compaction=False):
        content = payload["content"]
        execution = self._execution_context(actor_id)
        if manual:
            self._management_allowed(actor_id)
            return normalize_source(
                {
                    "kind": "manual",
                    "authority": "user",
                    "locator": {"entry_sha256": content_digest(content)},
                    "excerpt": content,
                    "excerpt_sha256": content_digest(content),
                    "artifact_refs": [],
                    "scope_ceiling": {"kind": "personal", "workspace_key": "", "item_id": ""},
                    "captured_at": _now(),
                }
            )
        session = getattr(self.engine, "_session", None)
        if session is None:
            raise PersonalAgentError("scope_denied", "请从实际对话提交记忆候选")
        profile = self.personal.current(session.id)
        actual_actor = profile.id if profile else "main"
        if actual_actor != actor_id or (
            session.personal_agent_id and session.personal_agent_id != actor_id
        ):
            raise PersonalAgentError("binding_conflict", "记忆候选与当前对话身份不一致")
        self.personal._session_valid(session)
        source_refs = list(getattr(self.engine, "_personal_artifact_refs", []))
        source_refs += list(getattr(self.engine, "_personal_memory_artifact_refs", []))
        locator = {
            "session_id": session.id,
            "workspace_key": self.workspace_key,
            "snapshot": "server_captured",
        }
        authority, kind, excerpt = "assistant", "compaction" if compaction else "message", content
        requested = payload.get("source_message_index")
        if requested is not None:
            integer(requested, "原消息位置", minimum=0)
            original = await self.engine.session_store.load(session.id)
            live = getattr(self.engine, "_full_history", None)
            history = (
                live
                if getattr(self.engine, "_personal_active_turn", None) and live
                else (original.messages if original is not None else [])
            )
            if requested >= len(history):
                raise PersonalAgentError("source_unavailable", "原始消息已变化，请重新选择来源")
            message = history[requested]
            raw = message.get("content")
            if not isinstance(raw, str):
                raise PersonalAgentError("invalid_input", "该来源消息不是文字")
            checksum = content_digest(raw)
            if payload.get("source_message_sha256") != checksum:
                raise PersonalAgentError("source_changed", "原消息摘要不一致，请核对真实来源")
            quote = payload.get("source_quote", raw[:8000])
            if not isinstance(quote, str) or not quote or quote not in raw or len(quote) > 8000:
                raise PersonalAgentError("source_changed", "来源摘录不是原消息中的确切文字")
            role = message.get("role", "unknown")
            authority = (
                "user" if role == "user" else "assistant" if role == "assistant" else "external"
            )
            if execution is not None or compaction:
                authority = "assistant"
            excerpt = quote
            locator["derivation"] = "exact_quote" if content == excerpt else "assistant_summary"
            locator.update(
                message_index=requested,
                message_id=str(message.get("id") or ""),
                role=role,
                message_sha256=checksum,
            )
            source_refs += (message.get("metadata") or {}).get("artifact_refs", [])
        elif payload.get("source_message_sha256") or payload.get("source_quote"):
            raise PersonalAgentError("invalid_input", "引用消息需要同时提供原位置和摘要")
        else:
            locator["proposal_sha256"] = content_digest(content)
        if execution is not None:
            locator.update(execution_task=True, origin_actor_id=execution["origin_actor_id"])
        artifact = payload.get("source_artifact_ref")
        if artifact is not None:
            exact = await self.engine.personal_artifacts.authorize_refs(
                actor_id=actor_id, refs=[artifact]
            )
            snapshot, raw = await self.engine.personal_artifacts.read(
                actor_id=actor_id, artifact_id=artifact["artifact_id"], version=artifact["version"]
            )
            source_refs += exact
            kind, authority = "artifact", "external"
            try:
                excerpt = raw.decode("utf-8-sig")[:8000] if is_text_artifact(snapshot) else ""
            except UnicodeError as exc:
                raise PersonalAgentError("source_unavailable", "来源文字编码发生变化") from exc
            locator.update(
                artifact_id=artifact["artifact_id"],
                artifact_version=artifact["version"],
                artifact_sha256=artifact["sha256"],
            )
        source_refs = _unique_refs(source_refs)
        if execution is not None:
            frozen = {canonical(ref) for ref in execution["artifact_refs"]}
            if any(canonical(ref) not in frozen for ref in source_refs):
                raise PersonalAgentError("scope_denied", "来源资料未包含在本次冻结输入中")
        if source_refs:
            await self.engine.personal_artifacts.authorize_refs(actor_id=actor_id, refs=source_refs)
        ceiling = {"kind": "workspace", "workspace_key": self.workspace_key, "item_id": ""}
        if execution is not None and execution["item_id"] is not None:
            ceiling = {
                "kind": "item",
                "workspace_key": self.workspace_key,
                "item_id": execution["item_id"],
            }
        elif scope["kind"] == "item":
            ceiling = scope
        dependencies = getattr(self.engine, "_personal_memory_runtime_refs", [])
        if not isinstance(dependencies, list) or len(dependencies) > 32:
            raise PersonalAgentError("scope_denied", "本次记忆依赖超过 32 条，请缩小上下文后再保存")
        unique = {canonical(normalize_ref(ref)): normalize_ref(ref) for ref in dependencies}
        if unique:
            locator.update(memory_refs=list(unique.values()), source_actor_id=actor_id)
        for ref in unique.values():
            dependency, _ = await self.resolve_ref(
                ref, actor_id, item_id=ceiling["item_id"] or None
            )
            if dependency["scope"]["kind"] == "item":
                if ceiling["kind"] == "item" and ceiling != dependency["scope"]:
                    raise PersonalAgentError("scope_denied", "记忆来源分属不同事项")
                ceiling = dependency["scope"]
        return normalize_source(
            {
                "kind": kind,
                "authority": authority,
                "locator": locator,
                "excerpt": excerpt,
                "excerpt_sha256": content_digest(excerpt),
                "artifact_refs": source_refs,
                "scope_ceiling": ceiling,
                "captured_at": _now(),
            }
        )

    @staticmethod
    def _human(channel, action, request_id):
        if channel not in _CHANNELS:
            raise PersonalAgentError("scope_denied", "请通过人工记忆面板或手动命令确认")
        return {
            "channel": _CHANNELS[channel],
            "actor_kind": "human",
            "action": action,
            "request_id": request_id,
        }

    async def _own(self, memory_id, actor_id):
        record = await self._call("get", memory_id=memory_id)
        if record["actor_id"] != actor_id or not self._scope_matches(record):
            raise PersonalAgentError("not_found", "该记忆不属于当前 Agent 和工作区")
        return record

    async def dispatch(
        self, operation, payload, *, actor_id="main", caller_actor=None, trusted_channel=None
    ):
        if not isinstance(operation, str):
            raise PersonalAgentError("invalid_input", "记忆操作格式不正确")
        payload = object_value(payload, "记忆操作")
        validate_operation_payload(
            operation, payload, manual=trusted_channel is not None or caller_actor is None
        )
        self._actor(actor_id, caller_actor=caller_actor, write=operation not in _READS)
        self._execution_context(actor_id)
        if operation in _READS:
            if trusted_channel is None and caller_actor is None:
                raise PersonalAgentError("scope_denied", "记忆读取缺少可信调用身份")
            if trusted_channel is not None:
                self._human(trusted_channel, operation, "memory-read")
            method = getattr(self, operation)
            result = await method(**payload, actor_id=actor_id, caller_actor=caller_actor)
            if caller_actor is not None and trusted_channel is None:
                values = result.get("memories", []) if isinstance(result, dict) else []
                if operation == "get":
                    values = [result]
                return {
                    "type": "personal_memory_refs",
                    "operation": operation,
                    "refs": [
                        v["ref"]
                        for v in values
                        if v.get("state") == "active" and v.get("source_available")
                    ],
                    "count": len(values),
                    "diagnostics": result.get("diagnostics", {}),
                    **{k: result[k] for k in ("limit", "offset", "next_offset") if k in result},
                }
            return result
        if operation not in HUMAN_OPERATIONS:
            raise PersonalAgentError("invalid_input", "记忆操作不支持此入口")
        if operation == "index_sync":
            self._management_allowed(actor_id)
            self._human(trusted_channel, operation, "index-sync")
            return await self.sync_index()
        if operation != "propose":
            self._management_allowed(actor_id)
            human = self._human(trusted_channel, operation, payload.get("request_id", ""))
        else:
            if caller_actor is None:
                self._management_allowed(actor_id)
                self._human(trusted_channel, operation, payload.get("request_id", ""))
            human = None
        request_id = payload.get("request_id")
        if request_id is None and operation == "propose" and caller_actor is not None:
            from naumi_agent.personal.runtime import current_run_id

            session = getattr(self.engine, "_session", None)
            run = current_run_id(self.engine, session.id) if session is not None else ""
            if not run:
                raise PersonalAgentError("invalid_input", "请为记忆候选保留稳定的请求编号")
            request_id = (
                "memory-proposal-"
                + digest({"run": run, "actor": actor_id, "payload": payload})[:40]
            )
            payload = {**payload, "request_id": request_id}
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        result = await self._commit(operation, payload, actor_id, human)
        if caller_actor is not None and trusted_channel is None:
            return {
                "type": "personal_memory_refs",
                "operation": operation,
                "refs": [],
                "candidate_id": result.get("memory_id"),
                "state": result.get("state"),
                "message": "候选已保存，人工确认后才会参与召回",
            }
        return result

    async def _commit(self, operation, payload, actor_id, human, *, compaction=False):
        request_id = payload["request_id"]
        async with self._mutation_lock:
            prepared = await self._call(
                "prepare_request",
                request_id=request_id,
                actor_id=actor_id,
                workspace_key=self.workspace_key,
                operation=operation,
                payload=payload,
                fingerprint=request_fingerprint(actor_id, self.workspace_key, operation, payload),
            )
            receipt = await self._call("find_request", request_id=request_id)
            if receipt is not None:
                if receipt["actor_id"] != actor_id:
                    raise PersonalAgentError(
                        "idempotency_conflict", "原记忆请求的所属 Agent 不一致"
                    )
                raw = receipt["result"]
                result = (
                    raw if receipt["result_kind"] == "grant" else await self._dto(raw, actor_id)
                )
            elif prepared["state"] == "erased":
                raise PersonalAgentError(
                    "memory_request_erased", "原请求正文已随记忆清理，请核对当前记录"
                )
            else:
                result = await self._mutate(
                    operation, payload, actor_id, human, compaction=compaction
                )
            minimal = {key: result[key] for key in ("memory_id", "grant_id") if key in result}
            await self._call("finish_request", request_id=request_id, result=minimal)
        self._wake.set()
        if operation not in {"propose", "import_legacy"}:
            await self.sync_index()
        return result

    async def propose_compaction(self, content, category, *, request_id):
        from naumi_agent.personal.runtime import require_current_agent

        profile = require_current_agent(self.engine)
        actor_id = profile.id if profile else "main"
        execution = self._execution_context(actor_id)
        item_id = execution["item_id"] if execution else None
        payload = {
            "request_id": request_id,
            "topic": "上下文压缩 · " + str(category),
            "category": category,
            "content": content,
            "scope": "item" if item_id else "workspace",
            **({"item_id": item_id} if item_id else {}),
        }
        validate_operation_payload("propose", payload, manual=True)
        return await self._commit("propose", payload, actor_id, None, compaction=True)

    async def _mutate(self, operation, p, actor_id, human, *, compaction=False):
        common = {"actor_id": actor_id, "request_id": p["request_id"]}
        if operation in {"create", "propose"}:
            scope = await self._scope(
                p.get("scope", "workspace"), p.get("item_id"), actor_id=actor_id
            )
            source = await self._capture_source(
                p, actor_id, scope, manual=operation == "create", compaction=compaction
            )
            result = await self._call(
                "create",
                **common,
                scope=scope,
                topic=p["topic"],
                category=p.get("category", "fact"),
                content=p["content"],
                source=source,
                human=human,
            )
            return await self._dto(result, actor_id)
        if operation == "import_legacy":
            legacy = next(
                (v for v in await self._legacy_records() if v["legacy_id"] == p["legacy_id"]), None
            )
            if legacy is None or legacy["content_sha256"] != p["expected_content_sha256"]:
                raise PersonalAgentError("source_changed", "原旧记忆不存在或内容已变化")
            scope = await self._scope(
                p.get("scope", "workspace"), p.get("item_id"), actor_id=actor_id
            )
            source = normalize_source(
                {
                    "kind": "legacy",
                    "authority": "unknown",
                    "locator": {
                        "legacy_id": legacy["legacy_id"],
                        "legacy_sha256": legacy["content_sha256"],
                    },
                    "excerpt": legacy["content"],
                    "excerpt_sha256": legacy["content_sha256"],
                    "artifact_refs": [],
                    "scope_ceiling": {
                        "kind": "workspace",
                        "workspace_key": self.workspace_key,
                        "item_id": "",
                    },
                    "captured_at": _now(),
                }
            )
            result = await self._call(
                "create",
                **common,
                scope=scope,
                topic=p["topic"],
                category=legacy["category"],
                content=legacy["content"],
                source=source,
            )
            return await self._dto(result, actor_id)
        if operation == "revoke":
            visible = False
            for record in await self._records(actor_id=actor_id):
                if not self._scope_matches(record):
                    continue
                grants = await self._call("grants", memory_id=record["memory_id"])
                if any(grant["grant_id"] == p["grant_id"] for grant in grants):
                    visible = True
                    break
            if not visible:
                raise PersonalAgentError("not_found", "该记忆分享不属于当前 Agent 和工作区")
            result = await self._call(
                "revoke",
                **common,
                grant_id=p["grant_id"],
                expected_revision=p["expected_revision"],
                human=human,
            )
            return result
        record = await self._own(p["memory_id"], actor_id)
        control = {
            **common,
            "memory_id": p["memory_id"],
            "expected_revision": p["expected_revision"],
        }
        if operation in {"delete", "reject"}:
            result = await self._call("control", **control, action=operation, human=human)
        elif operation == "confirm":
            await self._source_authorized(record, actor_id)
            result = await self._call(
                "confirm",
                **control,
                expected_content_sha256=p["expected_content_sha256"],
                expected_active_ref=p["expected_active_ref"],
                human=human,
            )
        elif operation == "revise":
            source = await self._capture_source(p, actor_id, record["scope"], manual=True)
            source["locator"].update(
                previous_source_sha256=record["source_sha256"],
                previous_content_sha256=record["content_sha256"],
            )
            result = await self._call(
                "revise",
                **control,
                content=p["content"],
                source=source,
                expected_content_sha256=p["expected_content_sha256"],
                human=human,
            )
        elif operation == "share":
            await self._source_authorized(record, actor_id)
            recipient = self._actor(p["recipient_actor_id"], write=True)
            if (
                type(p["recipient_version"]) is not int
                or self._version(recipient) != p["recipient_version"]
            ):
                raise PersonalAgentError("version_conflict", "接收 Agent 的配置已变化")
            source_refs = []
            for ref in record["source"]["artifact_refs"]:
                target = await self.engine.personal_artifacts.get(
                    actor_id=recipient,
                    artifact_id=ref["artifact_id"],
                    version=ref["version"],
                )
                target_ref = self.engine.personal_artifacts.reference(target)
                if target_ref["sha256"] != ref["sha256"]:
                    raise PersonalAgentError("source_changed", "接收 Agent 的来源资料版本不一致")
                source_refs.append(target_ref)
            await self._source_authorized(
                record,
                recipient,
                {"recipient_version": p["recipient_version"], "source_refs": source_refs},
            )
            return await self._call(
                "share",
                **control,
                expected_content_sha256=p["expected_content_sha256"],
                recipient_actor_id=recipient,
                recipient_version=p["recipient_version"],
                source_refs=source_refs,
                human=human,
            )
        else:
            raise PersonalAgentError("invalid_input", "记忆操作无效")
        return await self._dto(result, actor_id)

    async def prepare_request(self, value, *, trusted_channel):
        value = object_value(value, "记忆请求")
        if set(value) != {"request_id", "actor_id", "operation", "payload"}:
            raise PersonalAgentError("invalid_input", "记忆请求包含未知字段或缺少必要内容")
        actor = self._actor(value["actor_id"], write=True)
        self._management_allowed(actor)
        operation, payload = value["operation"], object_value(value["payload"], "记忆请求正文")
        self._human(trusted_channel, operation, value["request_id"])
        if operation not in HUMAN_OPERATIONS or operation == "index_sync":
            raise PersonalAgentError("invalid_input", "记忆请求操作无效")
        validate_operation_payload(operation, payload, manual=True)
        if payload.get("request_id") != value["request_id"]:
            raise PersonalAgentError("binding_conflict", "请求编号与原正文不一致")
        fp = request_fingerprint(actor, self.workspace_key, operation, payload)
        result = await self._call(
            "prepare_request",
            request_id=value["request_id"],
            actor_id=actor,
            workspace_key=self.workspace_key,
            operation=operation,
            payload=payload,
            fingerprint=fp,
        )
        return await self._request_dto(result, actor)

    def _request_identity(self, record, actor_id):
        if record["actor_id"] != actor_id or record["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "该原请求属于其他 Agent 或工作区")

    async def _request_dto(self, record, actor_id):
        self._request_identity(record, actor_id)
        result = {k: v for k, v in record.items() if k not in {"payload", "payload_sha256"}}
        raw = record.get("result")
        receipt = await self._call("find_request", request_id=record["request_id"])
        if receipt is not None:
            if receipt["actor_id"] != actor_id:
                raise PersonalAgentError("binding_conflict", "原记忆请求主体不一致")
            if receipt["result_kind"] == "grant":
                result["result"] = receipt["result"]
                return result
            raw = {"memory_id": receipt["memory_id"]}
        elif record["state"] == "erased" and record.get("target_memory_id"):
            raw = {"memory_id": record["target_memory_id"]}
        if isinstance(raw, dict) and raw.get("memory_id"):
            try:
                result["result"] = await self.get(raw["memory_id"], actor_id)
            except PersonalAgentError as exc:
                if exc.code != "not_found":
                    raise
                result["result"] = None
        return result

    async def request(self, request_id, *, actor_id="main"):
        self._actor(actor_id)
        self._management_allowed(actor_id)
        record = await self._call("get_prepared_request", request_id=request_id)
        return await self._request_dto(record, actor_id)

    async def apply_request(
        self, request_id, *, actor_id="main", expected_fingerprint, trusted_channel
    ):
        self._actor(actor_id, write=True)
        self._management_allowed(actor_id)
        self._human(trusted_channel, "apply", request_id)
        text(expected_fingerprint, "原请求摘要", 64, pattern=SHA256)
        record = await self._call("get_prepared_request", request_id=request_id)
        self._request_identity(record, actor_id)
        if record["fingerprint"] != expected_fingerprint:
            raise PersonalAgentError("binding_conflict", "原请求正文摘要不一致")
        if record.get("payload") is None:
            if (
                record["state"] == "erased"
                and await self._call("find_request", request_id=request_id) is None
            ):
                raise PersonalAgentError(
                    "memory_request_erased", "原请求正文已清理，请核对当前记忆"
                )
            projection = await self._request_dto(record, actor_id)
            return projection.get("result") or projection
        result = await self.dispatch(
            record["operation"],
            record["payload"],
            actor_id=actor_id,
            trusted_channel=trusted_channel,
        )
        return result

    async def discard_request(
        self, request_id, *, actor_id="main", expected_fingerprint, trusted_channel
    ):
        self._actor(actor_id, write=True)
        self._management_allowed(actor_id)
        self._human(trusted_channel, "discard", request_id)
        text(expected_fingerprint, "原请求摘要", 64, pattern=SHA256)
        async with self._mutation_lock:
            record = await self._call("get_prepared_request", request_id=request_id)
            self._request_identity(record, actor_id)
            result = await self._call(
                "discard_request", request_id=request_id, expected_fingerprint=expected_fingerprint
            )
            return {k: v for k, v in result.items() if k not in {"payload", "payload_sha256"}}

    async def context(self, actor_id="main"):
        self._actor(actor_id)
        self._execution_context(actor_id)
        await asyncio.to_thread(self._guard)
        records, _ = await self._eligible(actor_id)
        return {
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "actor_id": actor_id,
            "index": self.index.status(),
            "stats": {"active": len(records)},
            "automatic_recall_enabled": bool(self.engine.config.memory.long_term_enabled),
        }

    async def export(self, actor_id="main", scope="all", item_id=None, caller_actor=None):
        values, offset = [], 0
        while True:
            page = await self.list(
                actor_id, scope, item_id, limit=100, offset=offset, caller_actor=caller_actor
            )
            values.extend(
                v
                for v in page["memories"]
                if v.get("source_available")
                and v["state"] not in {"deleted", "rejected", "superseded"}
            )
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        return {
            "version": 1,
            "type": "personal_memory_export",
            "actor_id": actor_id,
            "workspace_key": self.workspace_key,
            "memories": values,
        }

    async def _legacy_records(self):
        legacy = getattr(self.engine, "_legacy_long_term_memory", None)
        if legacy is None or not Path(self.engine.config.memory.vector_db_path).exists():
            return []
        raw = await legacy.export_memories()
        values = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(values, dict):
            values = values.get("memories", [])
        result = []
        for value in values:
            content = value.get("content", "")
            if isinstance(content, str) and content and len(content) <= 8000:
                category = value.get("category", "fact")
                result.append(
                    {
                        "legacy_id": value["id"],
                        "content": content,
                        "content_sha256": content_digest(content),
                        "topic": content[:120],
                        "category": category if category in CATEGORIES else "fact",
                        "source_kind": "legacy",
                    }
                )
        return result

    async def legacy(self, actor_id="main", limit=50, offset=0):
        self._actor(actor_id)
        self._management_allowed(actor_id)
        integer(limit, "旧记忆页大小", maximum=100)
        integer(offset, "旧记忆位置", minimum=0)
        values = await self._legacy_records()
        return {
            "memories": values[offset : offset + limit],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if len(values) > offset + limit else None,
        }

    async def sync_index(self):
        async with self._sync_lock:
            processed, error = 0, ""
            events = await self._call("pending_index", limit=100)
            for event in events:
                try:
                    record = await self._call("get", memory_id=event["memory_id"])
                    if record["state"] == "active":
                        await asyncio.to_thread(self.index.upsert, record)
                    else:
                        await asyncio.to_thread(self.index.delete, record["memory_id"])
                    await self._call("ack_index", event_id=event["event_id"])
                    processed += 1
                except PersonalAgentError as exc:
                    if exc.code != "memory_index_unavailable":
                        raise
                    error = exc.message
                    break
            reconciled, removed = 0, 0
            if not error:
                records = await self._call("active_records")
                try:
                    reconciliation = await asyncio.to_thread(self.index.reconcile, records)
                    reconciled = reconciliation["repaired"]
                    removed = reconciliation["removed"]
                except PersonalAgentError as exc:
                    if exc.code != "memory_index_unavailable":
                        raise
                    error = exc.message
            return {
                **self.index.status(),
                "processed": processed,
                "reconciled": reconciled,
                "removed": removed,
                "error": error,
            }

    def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._index_loop())

    async def _index_loop(self):
        while True:
            self._wake.clear()
            try:
                if self.enabled:
                    await self.sync_index()
            except Exception as exc:
                logger.error("Memory index recovery failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def stop(self):
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
