"""Immutable read provenance rechecked at existing artifact/model execution boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.item_models import canonical, digest, keys
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_models import (
    item_ref,
    normalize_item_ref,
    normalize_metadata_ref,
    normalize_today_ref,
    record_digest,
    semantic_item_ref,
)

_WALK = ContextVar("personal_today_authority_walk", default=None)


@contextmanager
def _walking(service, identity):
    state, token = _WALK.get(), None
    if state is None:
        state = {"active": set(), "done": set(), "nodes": 0}
        token = _WALK.set(state)
    key = (service.owner_key, service.workspace_key, *identity)
    try:
        if key in state["active"]:
            raise PersonalAgentError("storage_error", "今日资料来源出现循环依赖")
        cached = key in state["done"]
        if not cached:
            state["nodes"] += 1
            if len(state["active"]) >= 32 or state["nodes"] > 4096:
                raise PersonalAgentError("input_limit", "今日资料来源超过校验容量")
            state["active"].add(key)
        try:
            yield cached
        except BaseException:
            raise
        else:
            if not cached:
                state["done"].add(key)
        finally:
            if not cached:
                state["active"].discard(key)
    finally:
        if token is not None:
            _WALK.reset(token)


class TodayAuthorityMixin:
    async def _capture_provenance(self, actor):
        if actor is None:
            return []
        await self._read_session(actor)
        routines = getattr(self.engine, "personal_routines", None)
        if routines is None:
            raise PersonalAgentError("source_unavailable", "计划变更缺少当前上下文授权组件")
        context = await routines._model_sources(actor)
        return [{"actor_id": actor, "context": context}]

    @staticmethod
    def _merge_provenance(record, extra, *, reset=False):
        values = ([] if reset else record.get("provenance", [])) + extra
        values = list({canonical(value): value for value in values}.values())
        if len(values) > 64:
            raise PersonalAgentError("input_limit", "计划来源上下文超过上限，请拆分安排")
        return values

    async def _authorize_metadata_sources(self, value, reader):
        contexts = value.get("provenance", [])
        if not isinstance(contexts, list) or len(contexts) > 64:
            raise self._broken()
        for source in contexts:
            keys(source, {"actor_id", "context"}, {"actor_id", "context"}, "原计划上下文")
            context = source["context"]
            keys(
                context,
                {"artifact_refs", "memory_refs", "memory_item_id", "context_session_ids"},
                {"artifact_refs", "memory_refs", "memory_item_id", "context_session_ids"},
                "原计划来源",
            )
            for actor in {source["actor_id"], reader} - {None}:
                await self._artifacts().authorize_refs(
                    actor_id=actor, refs=context["artifact_refs"]
                )
                memory = getattr(self.engine, "personal_memory", None)
                if context["memory_refs"] and memory is None:
                    raise PersonalAgentError("source_unavailable", "原计划记忆授权组件不可用")
                for ref in context["memory_refs"]:
                    await memory.resolve_ref(ref, actor, item_id=context["memory_item_id"])

    def _artifacts(self):
        result = getattr(self.engine, "personal_artifacts", None)
        if result is None or not callable(getattr(result, "authorize_refs", None)):
            raise PersonalAgentError("source_unavailable", "原计划资料需要当前授权组件")
        return result

    async def _authorize_item(self, raw, reader_actor, *, require_current=False):
        ref = normalize_item_ref(raw)
        if ref["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原计划事项不在当前工作区")
        identity = ("item", digest(ref), reader_actor, require_current)
        with _walking(self, identity):
            current = await self.items._item(ref["item_id"], reader_actor)
            history = await self.items._call("history", item_id=ref["item_id"])
            original = next((v for v in history if v["revision"] == ref["item_revision"]), None)
            if original is None or item_ref(original) != ref:
                raise PersonalAgentError("source_unavailable", "计划缺少对应的原事项修订")
            if original["definition"]["route"] != self.items._route():
                raise PersonalAgentError("source_unavailable", "原事项路由已失效")
            if require_current and semantic_item_ref(ref) != semantic_item_ref(item_ref(current)):
                raise PersonalAgentError("source_stale", "事项委托已改变，请刷新并明确重新绑定")
            artifacts = self._artifacts()
            refs = original["definition"]["inputs"]["artifact_refs"]
            assignee = original["definition"]["assignee"]["id"]
            intent = await self.items._call(
                "find_intent", item_id=ref["item_id"], once_key=ref["once_key"]
            )
            if intent:
                refs = [*refs, *intent["input_refs"]]
            refs = list({canonical(value): value for value in refs}.values())
            for actor in {assignee, reader_actor} - {None}:
                await artifacts.authorize_refs(actor_id=actor, refs=refs)
            return original, current

    @staticmethod
    def _day_reference(value):
        return normalize_today_ref(
            {
                "day_id": value["id"],
                "day_revision": value["revision"],
                "day_digest": record_digest(value),
                "workspace_key": value["workspace_key"],
                "planner_id": value["planner_id"],
                "date": value["date"],
                "timezone": value["timezone"],
                "timezone_sha256": value["timezone_sha256"],
            }
        )

    @staticmethod
    def _metadata_reference(value):
        return normalize_metadata_ref(
            {
                "metadata_id": value["id"],
                "metadata_revision": value["revision"],
                "metadata_digest": record_digest(value),
                "workspace_key": value["workspace_key"],
                "item_ref": value["item_ref"],
            }
        )

    async def _authorized_day_ref(self, raw, reader):
        ref = normalize_today_ref(raw)
        if ref["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("source_unavailable", "原日计划不在当前工作区")
        if ref["day_revision"]:
            value = await self._record("day", ref["day_id"], revision=ref["day_revision"])
        else:
            value = self._virtual_day(ref["date"], ref["timezone"], ref["planner_id"])
            value["timezone_sha256"] = ref["timezone_sha256"]
        self._day_scope(value, reader)
        if self._day_reference(value) != ref:
            raise PersonalAgentError("source_unavailable", "原日计划与确切历史修订不一致")
        await self._authorize_metadata_sources(value, reader)
        # Placement identities and exclusion tombstones contain no source body.
        # The read manifest binds exactly the bodies that were actually returned.
        return value

    async def _authorized_metadata_ref(self, raw, reader):
        ref = normalize_metadata_ref(raw)
        value = (
            (await self._record("metadata", ref["metadata_id"], revision=ref["metadata_revision"]))
            if ref["metadata_revision"]
            else self._virtual_metadata(ref["item_ref"])
        )
        if self._metadata_reference(self._metadata_scope(value)) != ref:
            raise PersonalAgentError("source_unavailable", "计划字段与原确切修订不一致")
        await self._authorize_metadata_sources(value, reader)
        await self._authorize_item(ref["item_ref"], reader)
        return value

    async def _read_authority(self, record, reader):
        if record["workspace_key"] != self.workspace_key or record["actor_id"] != reader:
            raise PersonalAgentError("source_unavailable", "今日资料与原读取 Agent 不一致")
        self._planner(caller_actor=reader)
        for ref in record["today_refs"]:
            await self._authorized_day_ref(ref, reader)
        for ref in record["metadata_refs"]:
            await self._authorized_metadata_ref(ref, reader)
        for ref in record["item_refs"]:
            await self._authorize_item(ref, reader)
        routines = getattr(self.engine, "personal_routines", None)
        if record["routine_refs"] and routines is None:
            raise PersonalAgentError("source_unavailable", "原周期来源组件不可用")
        for ref in record["routine_refs"]:
            original = await routines._authority_original(ref)
            routines._scope(original, reader)
            await routines.authorize_definition_sources(original["definition"], reader)
        for delivery in record["delivery_refs"]:
            intent = await self.items._call("get_intent", occurrence_id=delivery["occurrence_id"])
            if intent["actor_id"] != reader:
                raise PersonalAgentError("source_unavailable", "原结果不属于当前 Agent")
            await self._authorize_item(delivery["item_ref"], reader)
            view = await self.items.executor.view(intent)
            if not view.get("source_available", True):
                raise PersonalAgentError("source_unavailable", "原报告资料授权已失效")
            if any(
                view.get(key) != delivery.get(key)
                for key in (
                    "job_id",
                    "run_id",
                    "publication_id",
                    "delivery_id",
                    "delivery_sha256",
                    "delivered_at",
                )
            ):
                raise PersonalAgentError("source_unavailable", "原送达凭据与今日资料不一致")
            old_refs = normalize_refs(delivery.get("artifacts", []))
            if old_refs != normalize_refs(view.get("artifacts", [])):
                raise PersonalAgentError("source_unavailable", "原报告资料授权已变化")
            await self._artifacts().authorize_refs(actor_id=reader, refs=old_refs)

    async def authorize_artifact(self, snapshot, actor_id):
        artifacts = self._artifacts()

        async def visit(value):
            identity = (
                "artifact",
                value["artifact_id"],
                value["version"],
                value["sha256"],
                actor_id,
            )
            with _walking(self, identity) as cached:
                if cached:
                    return
                evidence = value.get("evidence")
                if not isinstance(evidence, dict):
                    raise self._broken()
                refs = evidence.get("today_read_refs", [])
                if not isinstance(refs, list) or len(refs) > 64:
                    raise self._broken()
                for ref in refs:
                    keys(
                        ref,
                        {"read_id", "revision", "digest", "workspace_key"},
                        {"read_id", "revision", "digest", "workspace_key"},
                        "原今日读取凭据",
                    )
                    if (
                        type(ref["revision"]) is not int
                        or ref["revision"] != 1
                        or ref["workspace_key"] != self.workspace_key
                        or type(ref["read_id"]) is not str
                        or re.fullmatch(r"tr_[a-f0-9]{32}", ref["read_id"]) is None
                        or type(ref["digest"]) is not str
                        or re.fullmatch(r"[a-f0-9]{64}", ref["digest"]) is None
                    ):
                        raise PersonalAgentError("source_unavailable", "原今日读取实例已失效")
                    record = await self._record("read", ref["read_id"], revision=1)
                    if record_digest(record) != ref["digest"]:
                        raise self._broken()
                    if record["body_sha256"] != value["sha256"]:
                        raise self._broken()
                    with _walking(self, ("read", ref["read_id"], actor_id)) as done:
                        if not done:
                            await self._read_authority(record, actor_id)
                for ref in normalize_refs(value.get("source_refs", [])):
                    parent = await artifacts._call(
                        "get",
                        actor_id=actor_id,
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
                    if parent["sha256"] != ref["sha256"]:
                        raise PersonalAgentError("source_unavailable", "原今日上游资料摘要不一致")
                    await visit(parent)

        await visit(snapshot)

    async def _read_session(self, actor, expected=None):
        from naumi_agent.personal.runtime import require_current_agent

        profile = require_current_agent(self.engine)
        actual = profile.id if profile else "main"
        session = getattr(self.engine, "_session", None)
        if actual != actor or session is None or expected is not None and session is not expected:
            raise PersonalAgentError("binding_conflict", "今日读取需要原 Agent 的有效对话")
        authorize = getattr(self.engine, "_personal_execution_authorizer", None)
        if authorize is not None:
            await authorize()
        if (
            await self._artifacts().session_actor(session.id, write=True, allow_execution=True)
            != actor
        ):
            raise PersonalAgentError("binding_conflict", "今日读取与持久对话归属不一致")
        return session, 0 if profile is None else profile.version

    async def _pin_body(
        self,
        body,
        actor,
        *,
        today_refs=(),
        metadata_refs=(),
        item_refs=(),
        routine_refs=(),
        delivery_refs=(),
    ):
        # Detach before the first await so the returned object equals the pinned bytes.
        body = json.loads(canonical(body))
        manifests = json.loads(
            canonical(
                {
                    "today_refs": list(today_refs),
                    "metadata_refs": list(metadata_refs),
                    "item_refs": list(item_refs),
                    "routine_refs": list(routine_refs),
                    "delivery_refs": list(delivery_refs),
                }
            )
        )
        session, version = await self._read_session(actor)
        session_id = session.id
        content = canonical(body).encode("utf-8")
        record = {
            "id": "tr_"
            + digest(
                {
                    "actor": actor,
                    "body": body,
                    "manifests": manifests,
                }
            )[:32],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "body_sha256": hashlib.sha256(content).hexdigest(),
            **manifests,
        }
        await self._read_authority(record, actor)

        def save(tx):
            old = tx.get("read", record["id"], missing=True)
            if old is not None:
                if {k: v for k, v in old.items() if k != "revision"} != record:
                    raise self._broken()
                return old
            return tx.put("read", record, expected_revision=0)

        await asyncio.to_thread(self._guard)
        self.personal.store.list()
        saved = await asyncio.to_thread(self.store.write, save)
        await asyncio.to_thread(self._guard)
        artifacts = self._artifacts()
        snapshot = await artifacts.publish(
            actor_id=actor,
            request_id="today-read:" + digest({"reader": actor, "read": saved["id"], "body": body}),
            title="今日安排",
            kind="material",
            filename="today-plan.txt",
            content=content,
            source_refs=[],
            evidence={
                "source_kind": "today_plan",
                "today_read_refs": [
                    {
                        "read_id": saved["id"],
                        "revision": 1,
                        "digest": record_digest(saved),
                        "workspace_key": self.workspace_key,
                    }
                ],
            },
        )
        if snapshot["sha256"] != saved["body_sha256"]:
            raise self._broken()
        await artifacts.bind_refs(session_id, actor, [artifacts.reference(snapshot)])
        _, latest_version = await self._read_session(actor, session)
        if latest_version != version or session.id != session_id:
            raise PersonalAgentError("binding_conflict", "读取期间 Agent 或对话发生变化")
        await self._read_authority(saved, actor)
        return body

    async def model_result(self, operation, result, actor, arguments):
        self._planner(caller_actor=actor)
        if operation in {"get", "metadata"}:
            from naumi_agent.personal.today_tools import execute_today_operation

            # Public projections are not authority; rebuild from the original query.
            query = json.loads(canonical(arguments))
            result = await execute_today_operation(self, query, caller_actor=actor)
            if operation == "metadata":
                metadata = result["metadata"]
                if result["entry"]["availability"] != "available":
                    return {
                        "metadata_id": result["metadata_id"],
                        "revision": result["revision"],
                        "availability": result["entry"]["availability"],
                    }
                return await self._pin_body(
                    result,
                    actor,
                    metadata_refs=[self._metadata_reference(metadata)],
                    item_refs=[result["entry"]["item_ref"]],
                    delivery_refs=[
                        {
                            key: value
                            for key, value in result["entry"]["report"].items()
                            if key not in {"title", "read_receipt"}
                        }
                    ]
                    if result["entry"].get("report")
                    else [],
                )
            result.pop("observed_at", None)
            result["entries"] = [
                row for row in result["entries"] if row["disposition"] != "exclude"
            ]
            refs, metas = [], []
            for entry in result["entries"]:
                if entry["availability"] != "available":
                    entry_keys = (
                        "id",
                        "placement_id",
                        "metadata_id",
                        "metadata_revision",
                        "item_ref",
                        "availability",
                        "planned",
                        "disposition",
                    )
                    entry_copy = {key: entry[key] for key in entry_keys}
                    entry.clear()
                    entry.update(entry_copy)
                    continue
                refs.append(entry["item_ref"])
                metadata = (
                    await self._record(
                        "metadata", entry["metadata_id"], revision=entry["metadata_revision"]
                    )
                    if entry["metadata_revision"]
                    else self._virtual_metadata(entry["item_ref"])
                )
                metas.append(self._metadata_reference(metadata))
            record = {k: v for k, v in result["day"].items() if k != "digest"}
            deliveries = {row["occurrence_id"]: row for row in result["deliveries"]}
            for entry in result["entries"]:
                if entry.get("report"):
                    deliveries[entry["report"]["occurrence_id"]] = entry["report"]
            return await self._pin_body(
                result,
                actor,
                today_refs=[self._day_reference(record)],
                metadata_refs=metas,
                item_refs=refs,
                routine_refs=[
                    row["plan_ref"]
                    for row in result["routines"]
                    if row["availability"] == "available"
                ],
                delivery_refs=[
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"title", "read_receipt"}
                    }
                    for row in deliveries.values()
                ],
            )
        if operation == "context":
            return {
                key: result[key]
                for key in ("workspace_key", "default_planner", "limits", "server_now")
            }
        if result.get("accepted"):
            return {
                key: result[key]
                for key in (
                    "accepted",
                    "request_id",
                    "operation",
                    "identity",
                    "projection_available",
                )
            }
        if operation == "requests":
            return {key: result[key] for key in ("status", "request_id")}
        if operation == "history":
            return {
                "history": [
                    {key: row[key] for key in ("id", "revision", "digest")}
                    for row in result["history"]
                ],
                "next_after_revision": result["next_after_revision"],
            }
        if operation in {"list", "sources"}:
            return {
                "entries": [
                    {
                        key: row[key]
                        for key in ("metadata_id", "metadata_revision", "availability", "item_ref")
                    }
                    for row in result["entries"]
                ],
                **{
                    key: result[key]
                    for key in ("next_cursor", "next_after", "partial")
                    if key in result
                },
            }
        raise PersonalAgentError("invalid_input", "今日操作未形成有效结果")
