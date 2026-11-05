"""Exact research material proofs, with current source and recipient reauthorization."""

from __future__ import annotations

import hashlib
import re
import uuid
from contextvars import ContextVar

from naumi_agent.personal.artifact_models import normalize_refs, strict_json_loads
from naumi_agent.personal.memory_models import normalize_ref as memory_ref
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest

_READ = re.compile(r"^rread_[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_WALKING: ContextVar[frozenset] = ContextVar("research_authority_walking", default=frozenset())


def normalize_read_ref(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"read_id", "revision", "workspace_key", "record_digest"}
        or type(value["revision"]) is not int
        or value["revision"] != 1
        or not isinstance(value["read_id"], str)
        or not _READ.fullmatch(value["read_id"])
        or any(
            not isinstance(value[key], str) or not _SHA.fullmatch(value[key])
            for key in ("workspace_key", "record_digest")
        )
    ):
        raise PersonalAgentError("invalid_input", "研究来源凭据格式不正确")
    return dict(value)


def research_evidence(evidence, *, egress=False):
    refs = evidence.get("research_egress_read_refs" if egress else "research_read_refs", [])
    if not isinstance(refs, list) or len(refs) > 64:
        raise PersonalAgentError("invalid_input", "研究来源凭据数量不正确")
    normalized = [normalize_read_ref(ref) for ref in refs]
    if len({canonical(ref) for ref in normalized}) != len(normalized):
        raise PersonalAgentError("invalid_input", "研究来源凭据重复")
    return bool(refs)


def read_ref(value):
    return {
        "read_id": value["id"],
        "revision": 1,
        "workspace_key": value["workspace_key"],
        "record_digest": digest(value),
    }


class ResearchAuthority:
    async def _basis(self, notebook, *, require_current, extra_source=None):
        current = await self._record("notebook", notebook["id"])
        if current["actor_id"] != notebook["actor_id"]:
            raise self._broken()
        if require_current and digest(current) != digest(notebook):
            raise PersonalAgentError("source_stale", "研究版本已变化，请重新读取")
        basis, refs, upstream, visited = [], [], [], set()

        async def walk(value, path):
            key = (value["id"], value["revision"])
            if key in path or len(path) >= 16 or len(visited) >= 64:
                raise PersonalAgentError("source_unavailable", "研究上游存在循环或超过校验深度")
            if key in visited:
                return
            visited.add(key)
            context_refs = value.get("context_refs", [])
            await self.artifacts.authorize_refs(actor_id=value["actor_id"], refs=context_refs)
            refs.extend(context_refs)
            cited = {}
            for claim in value.get("claims", []):
                for ref in claim.get("citations", []):
                    cited.setdefault(ref["source_id"], set()).add(ref["source_revision"])
            if extra_source is not None and extra_source["notebook_id"] == value["id"]:
                if (
                    extra_source["actor_id"] != value["actor_id"]
                    or extra_source["workspace_key"] != value["workspace_key"]
                ):
                    raise self._broken()
                cited.setdefault(extra_source["id"], set()).add(extra_source["revision"])
            for latest in await self._source_records(value["id"]):
                versions = cited.pop(latest["id"], set()) | {latest["revision"]}
                for revision in sorted(versions):
                    source = (
                        latest
                        if revision == latest["revision"]
                        else await self._record("source", latest["id"], revision=revision)
                    )
                    ref = source.get("artifact_ref")
                    if ref:
                        await self.artifacts.authorize_refs(actor_id=value["actor_id"], refs=[ref])
                        refs.append(ref)
                    basis.append(
                        {
                            "id": source["id"],
                            "revision": revision,
                            "digest": digest(source),
                            "latest_revision": latest["revision"],
                            "latest_digest": digest(latest),
                            "expired": self._expired(latest),
                        }
                    )
            if cited:
                raise PersonalAgentError("invalid_input", "引用来源不属于原研究")
            for claim in value.get("claims", []):
                for raw in claim.get("upstream_claims", []):
                    parent = await self._notebook(
                        raw["notebook_id"], value["actor_id"], revision=raw["revision"]
                    )
                    if not any(item["id"] == raw["claim_id"] for item in parent["claims"]):
                        raise PersonalAgentError("invalid_input", "上游结论不存在")
                    upstream.append({**raw, "record_digest": digest(parent)})
                    await walk(parent, path | {key})

        await walk(notebook, set())
        unique = {canonical(ref): ref for ref in refs}
        if len(unique) > 64:
            raise PersonalAgentError("input_limit", "研究资料超过 64 个确切版本")
        return {"notebook_digest": digest(notebook), "sources": basis, "upstream": upstream}, list(
            unique.values()
        )

    async def _new_read(
        self,
        body,
        notebook,
        actor,
        *,
        require_current,
        publication=False,
        extra_source=None,
        egress_context=None,
    ):
        content = body if isinstance(body, bytes) else canonical(body).encode("utf-8")
        if len(content) > 2 * 1024 * 1024:
            raise PersonalAgentError("input_limit", "研究结果超过单次读取容量")
        basis, refs = await self._basis(
            notebook, require_current=require_current, extra_source=extra_source
        )
        if egress_context is not None:
            if (
                type(egress_context) is not dict
                or set(egress_context) != {"artifact_refs", "memory_refs", "memory_item_id"}
                or not isinstance(egress_context["memory_refs"], list)
                or len(egress_context["memory_refs"]) > 64
            ):
                raise self._broken()
            egress_context = {
                "artifact_refs": normalize_refs(egress_context["artifact_refs"]),
                "memory_refs": [memory_ref(ref) for ref in egress_context["memory_refs"]],
                "memory_item_id": egress_context["memory_item_id"],
            }
            refs = normalize_refs(
                list(
                    {
                        canonical(ref): ref for ref in [*refs, *egress_context["artifact_refs"]]
                    }.values()
                )
            )
            await self.artifacts.authorize_refs(actor_id=actor, refs=refs)
        record = {
            "id": "rread_" + uuid.uuid4().hex,
            "revision": 1,
            "notebook_id": notebook["id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "status": "available",
            "created_at": self.stamp(),
            "notebook_revision": notebook["revision"],
            "basis": basis,
            "require_current": require_current,
            "publication": publication,
            "body_sha256": hashlib.sha256(content).hexdigest(),
            "source_refs": refs,
        }
        if egress_context is not None:
            record["egress_context"] = egress_context
            record["authority_kind"] = "approved_research_egress_v1"
        await self._write(lambda tx: tx.put("read", record, expected_revision=0))
        return record, content

    async def _read_authority(self, record, actor):
        self._actor(actor)
        self._actor(record["actor_id"])
        if record["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("source_unavailable", "研究来源不可用")
        if actor != record["actor_id"] and not record["publication"]:
            raise PersonalAgentError("source_unavailable", "研究读取与当前 Agent 不一致")
        key = (id(self), record["id"], actor)
        if key in _WALKING.get() or len(_WALKING.get()) >= 64:
            raise PersonalAgentError("source_unavailable", "研究来源存在循环或超过校验深度")
        token = _WALKING.set(_WALKING.get() | {key})
        try:
            notebook = await self._notebook(
                record["notebook_id"], record["actor_id"], revision=record["notebook_revision"]
            )
            # A notebook response names its exact revision and capture time. A
            # later edit or newly added source does not rewrite that old response.
            # Existing-source changes, expiry transitions and ACL loss do fence
            # live model material; explicitly historical reports retain live ACLs.
            if digest(notebook) != record["basis"]["notebook_digest"]:
                raise self._broken()
            if record["require_current"] and not record["publication"]:
                current = await self._notebook(notebook["id"], record["actor_id"])
                if digest(current) != digest(notebook):
                    raise PersonalAgentError("source_stale", "研究版本已变化")
            for ref in record["basis"]["sources"]:
                source = await self._record("source", ref["id"], revision=ref["revision"])
                if digest(source) != ref["digest"]:
                    raise self._broken()
                if not record["publication"]:
                    latest = await self._record("source", ref["id"])
                    if (
                        digest(latest) != ref["latest_digest"]
                        or self._expired(latest) != ref["expired"]
                    ):
                        raise PersonalAgentError("source_stale", "研究来源或时效已变化，请重新读取")
            for ref in record["basis"]["upstream"]:
                parent = await self._notebook(
                    ref["notebook_id"], record["actor_id"], revision=ref["revision"]
                )
                if digest(parent) != ref["record_digest"]:
                    raise self._broken()
            refs = record["source_refs"]
            await self.artifacts.authorize_refs(actor_id=record["actor_id"], refs=refs)
            context = record.get("egress_context")
            if context is not None:
                if (
                    record.get("authority_kind") != "approved_research_egress_v1"
                    or type(context) is not dict
                    or set(context) != {"artifact_refs", "memory_refs", "memory_item_id"}
                    or not isinstance(context["memory_refs"], list)
                    or len(context["memory_refs"]) > 64
                ):
                    raise self._broken()
                await self.artifacts.authorize_refs(
                    actor_id=record["actor_id"], refs=normalize_refs(context["artifact_refs"])
                )
                memory = getattr(self.engine, "personal_memory", None)
                if context["memory_refs"] and memory is None:
                    raise PersonalAgentError("source_unavailable", "研究的原记忆来源当前不可用")
                for raw in context["memory_refs"]:
                    ref = memory_ref(raw)
                    await memory.resolve_ref(
                        ref, record["actor_id"], item_id=context["memory_item_id"]
                    )
                    if actor != record["actor_id"]:
                        await memory.resolve_ref(ref, actor, item_id=context["memory_item_id"])
            # A shared report requires every exact upstream version for its reader.
            for ref in refs:
                snapshot = await self.artifacts.get(
                    actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
                )
                if snapshot["sha256"] != ref["sha256"]:
                    raise PersonalAgentError("source_stale", "研究资料摘要已变化")
        finally:
            _WALKING.reset(token)

    async def authorize_artifact(self, snapshot, actor_id):
        stack, seen = [snapshot], set()
        while stack:
            value = stack.pop()
            key = (value["artifact_id"], value["version"], value["sha256"])
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 4096:
                raise PersonalAgentError("source_unavailable", "研究来源超过校验上限")
            evidence = value.get("evidence", {})
            research_evidence(evidence)
            research_evidence(evidence, egress=True)
            for raw in [
                *evidence.get("research_read_refs", []),
                *evidence.get("research_egress_read_refs", []),
            ]:
                ref = normalize_read_ref(raw)
                record = await self._record("read", ref["read_id"], revision=1)
                if read_ref(record) != ref or value["sha256"] != record["body_sha256"]:
                    raise PersonalAgentError("source_unavailable", "研究正文与原凭据不一致")
                if (
                    raw in evidence.get("research_egress_read_refs", [])
                    and record.get("authority_kind") != "approved_research_egress_v1"
                ):
                    raise PersonalAgentError("source_unavailable", "外发材料与原上下文凭据不一致")
                await self._read_authority(record, actor_id)
            for ref in value.get("source_refs", []):
                original = await self.artifacts._call(
                    "get", actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
                )
                if original["sha256"] != ref["sha256"]:
                    raise PersonalAgentError("source_stale", "研究原资料版本已变化")
                stack.append(original)

    async def model_result(self, arguments, body, actor):
        if arguments["operation"].startswith("egress_"):
            return body
        if arguments["operation"] == "context":
            return body
        operation = arguments["operation"]
        if operation in {"list", "history"}:
            entries = body.get("notebooks" if operation == "list" else "history", [])
            if not entries:
                return body
            session = self.engine._session
            if session is None or await self.artifacts.session_actor(session.id) != actor:
                raise PersonalAgentError("scope_denied", "研究列表与当前对话不一致")
            records, refs = [], {}
            for entry in entries:
                notebook = await self._notebook(
                    entry["id"] if operation == "list" else arguments["id"],
                    actor,
                    revision=entry["revision"],
                )
                record, content = await self._new_read(body, notebook, actor, require_current=False)
                records.append(record)
                refs.update({canonical(ref): ref for ref in record["source_refs"]})
            if len(refs) > 64:
                raise PersonalAgentError("input_limit", "请缩小研究列表的读取范围")
            material = await self.artifacts.publish(
                actor_id=actor,
                request_id="research-list-" + records[0]["id"],
                title="研究列表确切读取",
                kind="material",
                filename="research-list.json",
                content=content,
                source_refs=list(refs.values()),
                evidence={"research_read_refs": [read_ref(record) for record in records]},
            )
            await self.artifacts.bind_refs(session.id, actor, [self.artifacts.reference(material)])
            if self.engine._session is not session:
                raise PersonalAgentError("source_stale", "读取研究期间对话已变化")
            for record in records:
                await self._read_authority(record, actor)
            return body
        extra_source = None
        if operation == "source":
            extra_source = await self._record(
                "source", arguments["id"], revision=arguments.get("revision")
            )
            notebook_id, revision = extra_source["notebook_id"], None
        else:
            view = body.get("view", body) if isinstance(body, dict) else {}
            returned = view.get("notebook", {})
            notebook_id, revision = returned.get("id"), returned.get("revision")
        if not notebook_id:
            return body
        notebook = await self._notebook(notebook_id, actor, revision=revision)
        session = self.engine._session
        if session is None:
            raise PersonalAgentError("scope_denied", "请从当前 Agent 的对话读取研究")
        session_id = session.id
        if await self.artifacts.session_actor(session_id) != actor:
            raise PersonalAgentError("binding_conflict", "研究与当前对话身份不一致")
        discovery_ref = (
            body.get("discovery", {}).get("artifact_ref") if isinstance(body, dict) else None
        )
        context = (
            {"artifact_refs": [discovery_ref], "memory_refs": [], "memory_item_id": None}
            if discovery_ref
            else None
        )
        record, content = await self._new_read(
            body,
            notebook,
            actor,
            require_current=False,
            extra_source=extra_source,
            egress_context=context,
        )
        material = await self.artifacts.publish(
            actor_id=actor,
            request_id="research-material-" + record["id"],
            title="研究确切读取",
            kind="material",
            filename="research.json",
            content=content,
            source_refs=record["source_refs"],
            evidence={
                "research_egress_read_refs" if context else "research_read_refs": [read_ref(record)]
            },
        )
        await self.artifacts.bind_refs(session_id, actor, [self.artifacts.reference(material)])
        if (
            self.engine._session is not session
            or await self.artifacts.session_actor(session_id) != actor
        ):
            raise PersonalAgentError("source_stale", "研究读取期间对话已变化")
        await self._read_authority(record, actor)
        return strict_json_loads(content)

    async def _pin_egress_result(self, body, proposal, actor):
        if body.get("availability") == "source_unavailable":
            return body
        session = self.engine._session
        if session is None or await self.artifacts.session_actor(session.id) != actor:
            raise PersonalAgentError("binding_conflict", "外发记录与当前对话身份不一致")
        notebook = await self._notebook(
            proposal["notebook_id"], actor, revision=proposal["notebook_revision"]
        )
        record, content = await self._new_read(
            body,
            notebook,
            actor,
            require_current=False,
            publication=True,
            egress_context=proposal["output_context"],
        )
        material = await self.artifacts.publish(
            actor_id=actor,
            request_id="research-egress-read-" + record["id"],
            title="研究外发确切读取",
            kind="material",
            filename="research-egress.json",
            content=content,
            source_refs=record["source_refs"],
            evidence={"research_egress_read_refs": [read_ref(record)]},
        )
        await self.artifacts.bind_refs(session.id, actor, [self.artifacts.reference(material)])
        if self.engine._session is not session:
            raise PersonalAgentError("source_stale", "外发记录读取期间对话已变化")
        await self._read_authority(record, actor)
        return strict_json_loads(content)
