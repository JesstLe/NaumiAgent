"""Immutable authoring inputs for notebook revisions, including raw Memory refs."""

from __future__ import annotations

import re
from contextvars import ContextVar

from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.memory_models import normalize_ref as normalize_memory_ref
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest

_ID = re.compile(r"^rpv_[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_SESSION = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_WALKING = ContextVar("research_authoring_provenance_walking", default=frozenset())
MAX_PROVENANCE_REFS = 256


def normalize_provenance_ref(value):
    if (
        type(value) is not dict
        or set(value) != {"id", "revision", "workspace_key", "record_digest"}
        or type(value.get("id")) is not str
        or _ID.fullmatch(value["id"]) is None
        or type(value.get("revision")) is not int
        or value["revision"] != 1
        or any(
            type(value.get(key)) is not str or _SHA.fullmatch(value[key]) is None
            for key in ("workspace_key", "record_digest")
        )
    ):
        raise PersonalAgentError("invalid_input", "研究写作依据凭据格式不正确")
    return dict(value)


def provenance_ref(record):
    return {
        "id": record["id"],
        "revision": 1,
        "workspace_key": record["workspace_key"],
        "record_digest": digest(record),
    }


class ResearchProvenanceMixin:
    async def _resolve_authoring_ref(self, raw):
        ref = normalize_provenance_ref(raw)
        if ref["workspace_key"] != self.workspace_key:
            raise self._broken()
        record = await self._record("provenance", ref["id"], revision=1)
        if provenance_ref(record) != ref:
            raise self._broken()
        return record

    async def _authorize_authoring_record(self, record, actor):
        from naumi_agent.personal.research_peer_authority import peer_provenance_allowed

        self._actor(actor)
        owner = self._actor(record["actor_id"])
        if record["workspace_key"] != self.workspace_key or (
            owner != actor and not peer_provenance_allowed(self, record, actor)
        ):
            raise PersonalAgentError("scope_denied", "研究写作依据不属于当前 Agent")
        if (
            record.get("origin_kind") not in {"human", "model"}
            or type(record.get("memory_refs")) is not list
            or len(record["memory_refs"]) > 64
            or record.get("memory_item_id") is not None
            and (
                type(record["memory_item_id"]) is not str
                or not 1 <= len(record["memory_item_id"]) <= 128
                or "\0" in record["memory_item_id"]
            )
        ):
            raise self._broken()
        origin = record.get("origin_session_id")
        if record["origin_kind"] == "model":
            if type(origin) is not str or _SESSION.fullmatch(origin) is None:
                raise self._broken()
        elif origin is not None or record["memory_refs"] or record["memory_item_id"] is not None:
            raise self._broken()
        # Historical author versions are provenance, not a perpetual version lock.
        # Actual actor scope and every original grant remain live on every read.
        refs = normalize_refs(record["artifact_refs"])
        normalize_refs(record["session_artifact_refs"], maximum=256)
        memories = [normalize_memory_ref(ref) for ref in record["memory_refs"]]
        if len({canonical(ref) for ref in memories}) != len(memories):
            raise self._broken()
        key = (self.workspace_key, record["id"], actor)
        if key in _WALKING.get() or len(_WALKING.get()) >= 64:
            raise PersonalAgentError("source_unavailable", "研究写作依据存在循环或超过校验深度")
        token = _WALKING.set(_WALKING.get() | {key})
        try:
            # A peer scope never replaces the original owner's live grants. The
            # enclosing exact peer input separately validates recipient Artifacts.
            await self.artifacts.authorize_refs(actor_id=owner, refs=refs)
            parent = getattr(self.engine, "_personal_semantic_parent", None)
            memory = getattr(parent or self.engine, "personal_memory", None)
            if memories and memory is None:
                raise PersonalAgentError("source_unavailable", "原研究记忆依据当前不可核验")
            for ref in memories:
                try:
                    await memory.resolve_ref(ref, owner, item_id=record["memory_item_id"])
                    if actor != owner:
                        await self._peer_memory(ref, actor, record["memory_item_id"])
                except PersonalAgentError as exc:
                    if exc.code == "storage_error":
                        raise
                    raise PersonalAgentError(
                        "source_unavailable", "原研究记忆已变化或不在当前范围"
                    ) from exc
        finally:
            _WALKING.reset(token)

    async def authorize_authoring_ref(self, raw, actor):
        record = await self._resolve_authoring_ref(raw)
        await self._authorize_authoring_record(record, actor)
        return record

    async def notebook_provenance(self, notebook, actor):
        if notebook["actor_id"] != actor or notebook["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("scope_denied", "研究写作依据与原研究身份不一致")
        self._actor(actor)
        raw_refs = notebook.get("authoring_provenance_refs")
        if raw_refs is None:
            # Old records stay readable; missing facts never become an empty proof.
            return {"status": "legacy_unknown", "records": []}
        if (
            type(raw_refs) is not list
            or not 1 <= len(raw_refs) <= MAX_PROVENANCE_REFS
            or type(notebook.get("authoring_provenance_legacy_unknown")) is not bool
        ):
            raise self._broken()
        refs = [normalize_provenance_ref(ref) for ref in raw_refs]
        if len({canonical(ref) for ref in refs}) != len(refs):
            raise self._broken()
        records = []
        for ref in refs:
            record = await self.authorize_authoring_ref(ref, actor)
            if (
                record["notebook_id"] != notebook["id"]
                or type(record.get("notebook_revision")) is not int
                or not 1 <= record["notebook_revision"] <= notebook["revision"]
            ):
                raise self._broken()
            records.append(record)
        return {
            "status": "legacy_unknown"
            if notebook["authoring_provenance_legacy_unknown"]
            else "complete",
            "records": records,
        }

    async def _prepare_authoring_provenance(
        self, notebook, actor, intent, *, from_model, previous=None
    ):
        if type(from_model) is not bool:
            raise PersonalAgentError("invalid_input", "研究写作入口格式不正确")
        inherited = (
            await self.notebook_provenance(previous, actor)
            if previous is not None
            else {"status": "complete", "records": []}
        )
        session_id, session_refs, memory_refs, item_id = None, [], [], None
        live_refs = list(notebook.get("context_refs", []))
        actor_version = (
            self.personal.require_active_profile(actor).version if actor != "main" else 0
        )
        if from_model:
            session = getattr(self.engine, "_session", None)
            if session is None or await self.artifacts.session_actor(session.id) != actor:
                raise PersonalAgentError("binding_conflict", "研究写作与当前对话身份不一致")
            session_id = session.id
            session_refs = await self.artifacts.session_refs(session_id, actor)
            raw = getattr(self.engine, "_personal_memory_runtime_refs", [])
            if type(raw) is not list or len(raw) > 64:
                raise PersonalAgentError("input_limit", "研究记忆依据超过 64 条")
            memory_refs = [normalize_memory_ref(ref) for ref in raw]
            if len({canonical(ref) for ref in memory_refs}) != len(memory_refs):
                raise PersonalAgentError("invalid_input", "研究记忆依据重复")
            runtime = getattr(self.engine, "personal_memory_runtime", None)
            if runtime is not None:
                await runtime.authorize()
                item_id = runtime.item_id()
            elif memory_refs:
                raise PersonalAgentError("source_unavailable", "原研究记忆运行组件当前不可用")
            live_refs.extend(getattr(self.engine, "_personal_memory_artifact_refs", []))
        live_refs = normalize_refs(list({canonical(ref): ref for ref in live_refs}.values()))
        record = {
            "id": "rpv_"
            + digest(
                {
                    "intent_id": intent["id"],
                    "notebook_id": notebook["id"],
                    "notebook_revision": notebook["revision"],
                    "workspace_key": self.workspace_key,
                }
            )[:32],
            "revision": 1,
            "notebook_id": notebook["id"],
            "notebook_revision": notebook["revision"],
            "actor_id": actor,
            "actor_version": actor_version,
            "workspace_key": self.workspace_key,
            "status": "recorded",
            "created_at": intent["created_at"],
            "origin_kind": "model" if from_model else "human",
            "origin_session_id": session_id,
            "intent_id": intent["id"],
            "artifact_refs": live_refs,
            "session_artifact_refs": session_refs,
            "memory_refs": memory_refs,
            "memory_item_id": item_id,
        }
        await self._authorize_authoring_record(record, actor)
        records = [*inherited["records"], record]
        if len(records) > MAX_PROVENANCE_REFS:
            raise PersonalAgentError("input_limit", "研究写作依据超过单份研究容量")
        notebook["authoring_provenance_refs"] = [provenance_ref(value) for value in records]
        notebook["authoring_provenance_legacy_unknown"] = inherited["status"] == "legacy_unknown"
        return record

    async def _recheck_authoring_provenance(self, notebook, record, actor, *, from_model):
        if type(from_model) is not bool or from_model != (record.get("origin_kind") == "model"):
            raise PersonalAgentError("binding_conflict", "原研究请求的写作入口不能切换")
        await self._authorize_authoring_record(record, actor)
        # Inherited inputs survive manual revisions and are checked before mutation.
        for ref in notebook["authoring_provenance_refs"][:-1]:
            await self.authorize_authoring_ref(ref, actor)
        if from_model:
            session = getattr(self.engine, "_session", None)
            if (
                session is None
                or session.id != record["origin_session_id"]
                or await self.artifacts.session_actor(session.id) != actor
            ):
                raise PersonalAgentError("binding_conflict", "保存研究期间对话身份已变化")
            refs = await self.artifacts.session_refs(session.id, actor)
            raw = getattr(self.engine, "_personal_memory_runtime_refs", [])
            runtime = getattr(self.engine, "personal_memory_runtime", None)
            current_item = runtime.item_id() if runtime is not None else None
            current_version = (
                self.personal.require_active_profile(actor).version if actor != "main" else 0
            )
            if (
                refs != record["session_artifact_refs"]
                or [normalize_memory_ref(ref) for ref in raw] != record["memory_refs"]
                or current_item != record["memory_item_id"]
                or current_version != record["actor_version"]
            ):
                raise PersonalAgentError("source_stale", "保存研究期间写作依据已变化")

    def _commit_authoring_provenance(self, tx, notebook, record):
        for ref in notebook["authoring_provenance_refs"][:-1]:
            value = tx.get("provenance", ref["id"], revision=1)
            if provenance_ref(value) != ref:
                raise self._broken()
        original = tx.get("provenance", record["id"], missing=True)
        if original is not None:
            if original != record:
                raise PersonalAgentError("binding_conflict", "原研究写作依据不能替换")
        else:
            tx.put("provenance", record, expected_revision=0)

    async def _read_provenance_records(self, basis):
        value = basis.get("authoring_provenance")
        if value is None:
            return []
        if (
            type(value) is not dict
            or set(value) != {"status", "refs"}
            or value["status"] not in {"complete", "legacy_unknown"}
        ):
            raise self._broken()
        if type(value["refs"]) is not list or len(value["refs"]) > 4096:
            raise self._broken()
        return [await self._resolve_authoring_ref(ref) for ref in value["refs"]]
