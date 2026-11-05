"""Explicit, byte-equivalent research context checkpoints over append-only pins."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import Field

from naumi_agent.personal.artifact_content import page_refs
from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest
from naumi_agent.personal.research_models import Digest, RequestId, StrictPayload

_ARCHIVED = ContextVar("research_context_archived_reads", default=frozenset())
_CONTEXTS = ContextVar("research_context_archived_checkpoints", default=frozenset())
_BUILDING = ContextVar("research_context_building", default=frozenset())
_SCOPED = ContextVar("research_context_scope", default=frozenset())
_ID = re.compile(r"^rcu_[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
ActorId = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
SessionId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]


class ContextPreview(StrictPayload):
    operation: Literal["context_preview"]
    session_id: SessionId
    actor_id: ActorId


class ContextRollforward(ContextPreview):
    operation: Literal["context_rollforward"]
    request_id: RequestId
    expected_context_revision: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
    expected_plan_digest: Digest


class ContextRequest(StrictPayload):
    operation: Literal["context_request"]
    request_id: RequestId


CONTEXT_OPERATIONS = {
    "context_preview": ContextPreview,
    "context_rollforward": ContextRollforward,
    "context_request": ContextRequest,
}


def normalize_context_ref(value):
    fields = {"id", "revision", "workspace_key", "record_digest", "original_ref"}
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value.get("id")) is not str
        or not _ID.fullmatch(value["id"])
        or type(value.get("revision")) is not int
        or value["revision"] != 1
        or any(
            type(value.get(k)) is not str or not _SHA.fullmatch(value[k])
            for k in ("workspace_key", "record_digest")
        )
    ):
        raise PersonalAgentError("invalid_input", "对话依据凭据格式不正确")
    return {**value, "original_ref": normalize_refs([value["original_ref"]])[0]}


def archived_read(service, record):
    return (
        service.workspace_key,
        record["actor_id"],
        record["id"],
        digest(record),
    ) in _ARCHIVED.get()


def _root_key(ref):
    return digest(normalize_refs([ref])[0])


class ResearchContextMixin:
    def _context_id(self, request_id):
        return "rcu_" + digest({"workspace_key": self.workspace_key, "request_id": request_id})[:32]

    def _context_head_id(self, session_id):
        return "rch_" + digest({"workspace_key": self.workspace_key, "session_id": session_id})[:32]

    async def _context_identity(self, session_id, actor_id, *, idle=False):
        actor = self._actor(actor_id)
        if await self.artifacts.session_actor(session_id, write=True) != actor:
            raise PersonalAgentError("binding_conflict", "对话依据与原 Agent 不一致")
        session = await self.engine.session_store.load(session_id)
        profile = self.personal.require_active_profile(actor) if actor != "main" else None
        if idle:
            active = getattr(self.engine, "_personal_active_turn", None)
            if active is not None and active[0] == session_id:
                raise PersonalAgentError("research_busy", "对话正在执行，请结束后更新依据")
            store = getattr(self.engine, "chat_run_store", None)
            if store is not None and store.db_path.is_file():
                # Nonterminal persisted executions require their original runtime
                # reconciliation; a different Engine must not infer that they ended.
                with sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True) as db:
                    exists = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_runs'"
                    ).fetchone()
                    if (
                        exists
                        and db.execute(
                            "SELECT 1 FROM chat_runs WHERE session_id=? "
                            "AND status='running' LIMIT 1",
                            (session_id,),
                        ).fetchone()
                    ):
                        raise PersonalAgentError("research_busy", "原执行尚未结束，请先核对原执行")
        return {
            "session_id": session_id,
            "actor_id": actor,
            "actor_version": profile.version if profile else 0,
            "actor_name": profile.name if profile else "主 Agent",
            "session_title": session.title or "未命名对话",
        }

    async def _context_graph(self, roots, actor):
        from naumi_agent.personal.research_authority import normalize_read_ref, read_ref

        nodes, reads, contexts = {}, {}, {}
        stack = list(roots)
        while stack:
            ref = normalize_refs([stack.pop()])[0]
            key = _root_key(ref)
            if key in nodes:
                continue
            if len(nodes) >= 4096:
                raise PersonalAgentError("input_limit", "对话依据超过校验容量")
            await self.artifacts._call("authorize_refs", actor_id=actor, refs=[ref])
            value = await self.artifacts._call(
                "get", actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            if value["sha256"] != ref["sha256"]:
                raise self._broken()
            nodes[key] = value
            evidence = value.get("evidence", {})
            for raw in [
                *evidence.get("research_read_refs", []),
                *evidence.get("research_egress_read_refs", []),
            ]:
                proof = normalize_read_ref(raw)
                record = await self._record("read", proof["read_id"], revision=1)
                if read_ref(record) != proof or record["body_sha256"] != value["sha256"]:
                    raise self._broken()
                # Shared foreign live research is not an owner-controlled checkpoint.
                if not record["publication"] and record["actor_id"] != actor:
                    raise PersonalAgentError("scope_denied", "原研究依据需要所属 Agent 更新")
                reads[record["id"]] = record
            for raw in evidence.get("research_context_refs", []):
                proof = normalize_context_ref(raw)
                record = await self._record("context_update", proof["id"], revision=1)
                if (
                    digest(record) != proof["record_digest"]
                    or record["workspace_key"] != self.workspace_key
                ):
                    raise self._broken()
                if record["actor_id"] != actor:
                    raise PersonalAgentError("scope_denied", "原对话依据属于其他 Agent")
                contexts[record["id"]] = record
            stack.extend(value.get("source_refs", []))
        return nodes, reads, contexts

    @asynccontextmanager
    async def _context_archive_scope(self, reads, contexts):
        token = _ARCHIVED.set(
            _ARCHIVED.get()
            | frozenset((self.workspace_key, r["actor_id"], r["id"], digest(r)) for r in reads)
        )
        old = _CONTEXTS.set(_CONTEXTS.get() | frozenset((r["id"], digest(r)) for r in contexts))
        try:
            yield
        finally:
            _CONTEXTS.reset(old)
            _ARCHIVED.reset(token)

    async def _context_checkpoints(self, reads, contexts):
        source_ids = {s["id"] for r in reads for s in r["basis"]["sources"]}
        source_ids.update(s["id"] for r in contexts for s in r["checkpoints"])
        result = []
        for source_id in sorted(source_ids):
            source = await self._record("source", source_id)
            result.append(
                {
                    "id": source_id,
                    "revision": source["revision"],
                    "digest": digest(source),
                    "expired": self._expired(source),
                }
            )
        return result

    async def _context_plan(self, session_id, actor_id):
        identity = await self._context_identity(session_id, actor_id)
        roots = await self.artifacts._call("session_refs", actor_id=actor_id, session_id=session_id)
        head = await self._record("context_head", self._context_head_id(session_id), missing=True)
        nodes, reads, contexts = await self._context_graph(roots, actor_id)
        if len(reads) > 256 or len(contexts) > 256:
            raise PersonalAgentError("input_limit", "研究依据过多，请拆分对话")
        async with self._context_archive_scope(reads.values(), contexts.values()):
            for root in roots:
                await self.artifacts.get(
                    actor_id=actor_id, artifact_id=root["artifact_id"], version=root["version"]
                )
        checkpoints = await self._context_checkpoints(reads.values(), contexts.values())
        checkpoint_map = {r["id"]: r for r in checkpoints}
        changes = {}
        for read in reads.values():
            if read["publication"]:
                continue
            for source in read["basis"]["sources"]:
                current = checkpoint_map[source["id"]]
                if (
                    current["digest"] != source["latest_digest"]
                    or current["expired"] != source["expired"]
                ):
                    key = (read["notebook_id"], source["id"], source["latest_revision"])
                    changes[key] = {
                        "notebook_id": read["notebook_id"],
                        "source_id": source["id"],
                        "from_revision": source["latest_revision"],
                        "to_revision": current["revision"],
                        "from_expired": source["expired"],
                        "to_expired": current["expired"],
                    }
        material_roots = []
        for root in roots:
            _, descendants, old_contexts = await self._context_graph([root], actor_id)
            if descendants or old_contexts:
                material_roots.append(root)
        observations = {
            "identity": identity,
            "roots": roots,
            "reads": sorted(reads.values(), key=lambda r: r["id"]),
            "archived_contexts": sorted(contexts.values(), key=lambda r: r["id"]),
            "checkpoints": checkpoints,
            "material_roots": material_roots,
        }
        observations_digest = digest(observations)
        revision = head["revision"] if head else 0
        current = not changes and not contexts
        if head and head.get("observations_digest") == observations_digest:
            current = True
        elif head:
            try:
                effective = await self.effective_context_refs(session_id, actor_id, roots)
                await self.artifacts.authorize_refs(actor_id=actor_id, refs=effective)
                current = True
            except PersonalAgentError as exc:
                if exc.code != "source_stale":
                    raise
                current = False
        plan = {
            **observations,
            "context_revision": revision,
            "observations_digest": observations_digest,
            "changes": list(changes.values()),
        }
        plan["plan_digest"] = digest(plan)
        view = {
            "workspace_key": self.workspace_key,
            **identity,
            "context_revision": revision,
            "plan_digest": plan["plan_digest"],
            "status": "current" if current else "ready",
            "materials": [
                {
                    "original_ref": ref,
                    "title": nodes[_root_key(ref)]["title"],
                    "body_sha256": ref["sha256"],
                    "read_count": len(reads),
                }
                for ref in material_roots
            ],
            "changes": plan["changes"],
            "historical_notice": "旧资料保留原文并作为历史依据；来源已变化，相关判断仍需重新核实。",
        }
        return plan, view

    async def context_preview(self, args, *, caller_actor=None, trusted_channel=None):
        if caller_actor is not None:
            session = getattr(self.engine, "_session", None)
            if (
                caller_actor != args["actor_id"]
                or session is None
                or session.id != args["session_id"]
            ):
                raise PersonalAgentError("scope_denied", "只能核对当前 Agent 的原对话依据")
        _, view = await self._context_plan(args["session_id"], args["actor_id"])
        return view

    async def _authorize_context_record(self, record, actor, *, historical=False):
        if record["workspace_key"] != self.workspace_key or record["actor_id"] != actor:
            raise PersonalAgentError("scope_denied", "对话依据与原身份不一致")
        identity = await self._context_identity(record["session_id"], actor)
        if not historical and identity["actor_version"] != record["actor_version"]:
            raise PersonalAgentError("source_stale", "Agent 已更新，请重新核对对话依据")
        if not historical:
            for checkpoint in record["checkpoints"]:
                current = await self._record("source", checkpoint["id"])
                if (
                    digest(current) != checkpoint["digest"]
                    or self._expired(current) != checkpoint["expired"]
                ):
                    raise PersonalAgentError("source_stale", "来源再次变化，请重新更新对话依据")
        # Immutable original read records and their hashes are checked again on every use.
        for read in record["reads"]:
            if await self._record("read", read["id"], revision=1) != read:
                raise self._broken()

    async def _context_proof(self, proof, actor):
        proof = normalize_context_ref(proof)
        record = await self._record("context_update", proof["id"], revision=1)
        if proof["workspace_key"] != self.workspace_key or digest(record) != proof["record_digest"]:
            raise self._broken()
        if proof["original_ref"] not in record["material_roots"]:
            raise self._broken()
        historical = (record["id"], digest(record)) in _CONTEXTS.get()
        await self._authorize_context_record(record, actor, historical=historical)
        result = await self._record("context_result", record["id"], missing=True)
        if result is None and record["id"] not in _BUILDING.get():
            raise PersonalAgentError("source_unavailable", "对话依据尚未完成保存，请核对原请求")
        return record

    @asynccontextmanager
    async def artifact_context_scope(self, snapshot, actor):
        key = (snapshot["artifact_id"], snapshot["version"], actor)
        if key in _SCOPED.get() or any(entry[0] == self.workspace_key for entry in _ARCHIVED.get()):
            yield
            return
        # Discover before installing scope. Only explicit, immutable receipts can
        # authorize freshness exceptions; direct old reads never acquire them.
        accepted, proofs = [], []
        stack = [snapshot]
        seen = set()
        while stack:
            value = stack.pop()
            identity = (value["artifact_id"], value["version"])
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 4096:
                raise PersonalAgentError("input_limit", "对话依据超过校验容量")
            for raw in value.get("evidence", {}).get("research_context_refs", []):
                proof = normalize_context_ref(raw)
                record = await self._record("context_update", proof["id"], revision=1)
                if (
                    digest(record) != proof["record_digest"]
                    or record["workspace_key"] != self.workspace_key
                ):
                    raise self._broken()
                done = await self._record("context_result", record["id"], missing=True)
                if done is None and record["id"] not in _BUILDING.get():
                    raise PersonalAgentError("source_unavailable", "对话依据尚未完成保存")
                if value["sha256"] != raw["original_ref"]["sha256"]:
                    raise self._broken()
                content = await self.artifacts._call(
                    "read_content",
                    actor_id=actor,
                    artifact_id=value["artifact_id"],
                    version=value["version"],
                )
                expected = {
                    canonical(ref)
                    for ref in [raw["original_ref"], *page_refs(content, value["suffix"])]
                }
                if {canonical(ref) for ref in value["source_refs"]} != expected:
                    raise self._broken()
                accepted.append(record)
                proofs.append(proof)
            for ref in value.get("source_refs", []):
                stack.append(
                    await self.artifacts._call(
                        "get",
                        actor_id=actor,
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
                )
        if not accepted:
            yield
            return
        reads = {r["id"]: r for c in accepted for r in c["reads"]}
        archived = {r["id"]: r for c in accepted for r in c["archived_contexts"]}
        token = _SCOPED.set(_SCOPED.get() | {key})
        try:
            async with self._context_archive_scope(reads.values(), archived.values()):
                for proof in proofs:
                    await self._context_proof(proof, actor)
                yield
        finally:
            _SCOPED.reset(token)

    async def authorize_context_artifact(self, snapshot, actor):
        for proof in snapshot.get("evidence", {}).get("research_context_refs", []):
            await self._context_proof(proof, actor)

    async def effective_context_refs(self, session_id, actor_id, refs):
        head = await self._record("context_head", self._context_head_id(session_id), missing=True)
        if head is None:
            return refs
        identity = await self._context_identity(session_id, actor_id)
        if identity["actor_version"] != head["actor_version"]:
            raise PersonalAgentError("source_stale", "Agent 已变化，请重新核对对话依据")
        result = await self._record("context_result", head["update_id"])
        if head["mappings"] != result["mappings"] or result["session_id"] != session_id:
            raise self._broken()
        effective = {
            canonical(value): value
            for ref in refs
            for value in [head["mappings"].get(_root_key(ref), ref)]
        }
        return normalize_refs(list(effective.values()), maximum=256)

    async def context_rollforward(self, args, *, caller_actor=None, trusted_channel=None):
        if caller_actor is not None or trusted_channel not in {"web", "cli", "tui"}:
            raise PersonalAgentError("scope_denied", "更新对话依据需要用户明确确认")
        identity = await self._context_identity(args["session_id"], args["actor_id"], idle=True)
        update_id, fingerprint = self._context_id(args["request_id"]), digest(args)
        existing = await self._record("context_update", update_id, missing=True)
        if existing is not None and existing["fingerprint"] != fingerprint:
            raise PersonalAgentError("idempotency_conflict", "原请求已对应其他对话依据")
        completed = await self._record("context_result", update_id, missing=True)
        if completed is not None:
            await self._authorize_context_record(existing, args["actor_id"])
            await self.artifacts.session_refs(args["session_id"], args["actor_id"])
            return completed["receipt"]
        plan, view = await self._context_plan(args["session_id"], args["actor_id"])
        if (
            plan["context_revision"] != args["expected_context_revision"]
            or plan["plan_digest"] != args["expected_plan_digest"]
        ):
            raise PersonalAgentError("source_stale", "对话依据已变化，请重新核对预览")
        record = existing or {
            "id": update_id,
            "revision": 1,
            "notebook_id": "",
            "workspace_key": self.workspace_key,
            **identity,
            "status": "prepared",
            "created_at": self.stamp(),
            "request_id": args["request_id"],
            "fingerprint": fingerprint,
            "raw_refs_digest": digest(plan["roots"]),
            "plan_digest": plan["plan_digest"],
            "observations_digest": plan["observations_digest"],
            "expected_revision": plan["context_revision"],
            "reads": plan["reads"],
            "archived_contexts": plan["archived_contexts"],
            "checkpoints": plan["checkpoints"],
            "material_roots": plan["material_roots"],
            "changes": view["changes"],
            "view": view,
        }
        if existing is None:

            def reserve(tx):
                prior = tx.get("context_update", update_id, missing=True)
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise PersonalAgentError("idempotency_conflict", "原请求内容不一致")
                    return prior
                return tx.put("context_update", record, expected_revision=0)

            record = await self._write(reserve)
        mappings = {}
        should_update = view["status"] == "ready"
        token = _BUILDING.set(_BUILDING.get() | {update_id})
        try:
            if should_update:
                async with self._context_archive_scope(
                    record["reads"], record["archived_contexts"]
                ):
                    for original in record["material_roots"]:
                        value, body = await self.artifacts.read(
                            actor_id=args["actor_id"],
                            artifact_id=original["artifact_id"],
                            version=original["version"],
                        )
                        if hashlib.sha256(body).hexdigest() != original["sha256"]:
                            raise self._broken()
                        proof = {
                            "id": update_id,
                            "revision": 1,
                            "workspace_key": self.workspace_key,
                            "record_digest": digest(record),
                            "original_ref": original,
                        }
                        mirror = await self.artifacts.publish(
                            actor_id=args["actor_id"],
                            request_id="context-mirror-"
                            + update_id
                            + "-"
                            + _root_key(original)[:32],
                            title=value["title"],
                            kind=value["kind"],
                            filename="context" + value["suffix"],
                            content=body,
                            source_refs=[original],
                            evidence={
                                "research_context_refs": [proof],
                                "source_kind": "research_context_rollforward",
                                "semantic_verification": "not_performed",
                            },
                        )
                        mappings[_root_key(original)] = self.artifacts.reference(mirror)
            await self._authorize_context_record(record, args["actor_id"])
            fresh_plan, _ = await self._context_plan(args["session_id"], args["actor_id"])
            if fresh_plan["plan_digest"] != plan["plan_digest"]:
                raise PersonalAgentError("source_stale", "保存期间对话依据已变化，请核对原请求")
            await self._context_identity(args["session_id"], args["actor_id"], idle=True)
            revision = plan["context_revision"] + int(should_update)
            receipt = {
                "status": "completed",
                "request_id": args["request_id"],
                "workspace_key": self.workspace_key,
                "session_id": args["session_id"],
                "actor_id": args["actor_id"],
                "context_revision": revision,
                "updated_count": len(mappings),
                "view": {**view, "context_revision": revision, "status": "current"},
            }

            def commit(tx):
                done = tx.get("context_result", update_id, missing=True)
                if done is not None:
                    return done["receipt"]
                head_id = self._context_head_id(args["session_id"])
                head = tx.get("context_head", head_id, missing=True)
                if (head["revision"] if head else 0) != plan["context_revision"]:
                    raise PersonalAgentError("version_conflict", "其他请求已更新对话依据")
                for checkpoint in record["checkpoints"]:
                    current = tx.get("source", checkpoint["id"])
                    if (
                        digest(current) != checkpoint["digest"]
                        or self._expired(current) != checkpoint["expired"]
                    ):
                        raise PersonalAgentError("source_stale", "提交前来源已变化，请核对原请求")
                for read in record["reads"]:
                    if tx.get("read", read["id"], revision=1) != read:
                        raise self._broken()
                result = {
                    "id": update_id,
                    "notebook_id": "",
                    "workspace_key": self.workspace_key,
                    **identity,
                    "status": "completed",
                    "mappings": mappings,
                    "receipt": receipt,
                }
                tx.put("context_result", result, expected_revision=0)
                if should_update:
                    tx.put(
                        "context_head",
                        {
                            "id": head_id,
                            "notebook_id": "",
                            "workspace_key": self.workspace_key,
                            **identity,
                            "status": "current",
                            "update_id": update_id,
                            "mappings": mappings,
                            "observations_digest": plan["observations_digest"],
                        },
                        expected_revision=plan["context_revision"],
                    )
                return receipt

            result = await self._write(commit)
        finally:
            _BUILDING.reset(token)
        await self.artifacts.session_refs(args["session_id"], args["actor_id"])
        return result

    async def context_request(self, args, *, caller_actor=None, trusted_channel=None):
        record = await self._record(
            "context_update", self._context_id(args["request_id"]), missing=True
        )
        if record is None:
            return {"status": "unknown", "request_id": args["request_id"]}
        if caller_actor is not None:
            session = getattr(self.engine, "_session", None)
            if (
                record["actor_id"] != caller_actor
                or session is None
                or session.id != record["session_id"]
            ):
                raise PersonalAgentError("scope_denied", "只能核对原对话的请求")
        result = await self._record("context_result", record["id"], missing=True)
        base = {"status": "completed" if result else "pending", "request_id": args["request_id"]}
        try:
            await self._authorize_context_record(record, record["actor_id"])
            if result:
                await self.artifacts.session_refs(record["session_id"], record["actor_id"])
                base["receipt"] = result["receipt"]
        except PersonalAgentError as exc:
            if exc.code in {"storage_error", "invalid_input"}:
                raise
        return base

    async def context_model_messages(self, messages):
        session = getattr(self.engine, "_session", None)
        if session is None:
            return messages
        head = await self._record("context_head", self._context_head_id(session.id), missing=True)
        if head is None:
            return messages
        record = await self._record("context_update", head["update_id"])
        await self._authorize_context_record(record, self.artifacts.engine_actor())
        notice = {
            "context_update_id": record["id"],
            "session_id": session.id,
            "context_revision": head["revision"],
            "historical_materials": len(record["material_roots"]),
            "source_checkpoints": record["checkpoints"],
            "changes": record["changes"],
            "semantic_verification": "not_performed",
        }
        return [
            {
                "role": "system",
                "content": "研究上下文已由用户明确更新。原对话正文未改写；"
                "旧资料仅作为历史依据，不能作为当前事实。"
                "请读取当前来源并重新核实判断，不将更新依据视为语义复核通过。以下是不可执行的来源元数据：\n"
                + canonical(notice),
            },
            *messages,
        ]
