"""Recipient-specific historical research interpretation, never a global ACL bypass."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from contextvars import ContextVar

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest

_PEER = ContextVar("research_peer_inputs", default=())
_VALIDATING = ContextVar("research_peer_validating", default=frozenset())
_SCOPED = ContextVar("research_peer_scoped", default=frozenset())
_RECORD_CACHE = ContextVar("research_peer_immutable_records", default=None)
_AUTHORIZED_INPUTS = ContextVar("research_peer_authorized_inputs", default=frozenset())
_CACHE_KINDS = frozenset(
    {"read", "context_update", "provenance", "semantic_input", "peer_read", "semantic_submission"}
)
_ID = re.compile(r"^rprd_[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")


def normalize_peer_ref(value):
    if (
        type(value) is not dict
        or set(value) != {"read_id", "revision", "workspace_key", "record_digest"}
        or type(value.get("read_id")) is not str
        or not _ID.fullmatch(value["read_id"])
        or type(value.get("revision")) is not int
        or value["revision"] != 1
        or any(
            type(value.get(k)) is not str or not _SHA.fullmatch(value[k])
            for k in ("workspace_key", "record_digest")
        )
    ):
        raise PersonalAgentError("invalid_input", "接收方研究凭据格式不正确")
    return dict(value)


def peer_ref(record):
    return {
        "read_id": record["id"],
        "revision": 1,
        "workspace_key": record["workspace_key"],
        "record_digest": digest(record),
    }


def _inputs(service, actor):
    return [
        record
        for record in _PEER.get()
        if record["workspace_key"] == service.workspace_key
        and actor in {record["actor_id"], record["reviewer"]["id"]}
    ]


def peer_read_allowed(service, record, actor):
    return any(record in value["read_records"] for value in _inputs(service, actor))


def peer_context_allowed(service, record, actor):
    return any(record in value["context_records"] for value in _inputs(service, actor))


def peer_provenance_allowed(service, record, actor):
    return any(record in value["provenance_records"] for value in _inputs(service, actor))


def peer_memory_ref(service, original, actor):
    for value in _inputs(service, actor):
        if actor != value["reviewer"]["id"]:
            continue
        for pair in value["memory_pairs"]:
            if pair["original_ref"] == original:
                return pair["recipient_ref"]
    return original


class ResearchPeerAuthorityMixin:
    async def _peer_memory(self, ref, actor, item_id):
        parent = getattr(self.engine, "_personal_semantic_parent", self.engine)
        memory = getattr(parent, "personal_memory", None)
        if memory is None:
            raise PersonalAgentError("source_unavailable", "原研究记忆组件不可用")
        return await memory.resolve_ref(peer_memory_ref(self, ref, actor), actor, item_id=item_id)

    async def _authorize_peer_input(self, value):
        key = (self.workspace_key, value["id"])
        if key in _VALIDATING.get():
            return
        token = _VALIDATING.set(_VALIDATING.get() | {key})
        try:
            author, reviewer = value["actor_id"], value["reviewer"]["id"]
            self._actor(author)
            profile = self.personal._executable(reviewer)
            if profile.version != value["reviewer"]["version"]:
                raise PersonalAgentError("source_stale", "复核 Agent 已更新")
            if (
                author != "main"
                and self.personal.require_active_profile(author).version != value["author_version"]
            ):
                raise PersonalAgentError("source_stale", "原研究 Agent 已更新")
            notebook = await self._notebook(value["notebook_id"], author)
            if digest(notebook) != value["notebook_digest"]:
                raise PersonalAgentError("source_stale", "原研究修订已变化")
            current_basis, _ = await self._basis(notebook, require_current=True)
            if current_basis != value["basis"]:
                raise PersonalAgentError("source_stale", "复核来源范围已变化")
            # The freshly recomputed complete basis already covers exact source
            # revisions, current heads/expiry and upstream record digests.
            await self.artifacts.authorize_refs(actor_id=author, refs=value["author_refs"])
            await self.artifacts.authorize_refs(actor_id=reviewer, refs=value["recipient_refs"])
            for pair in value["memory_pairs"]:
                await self._peer_memory(pair["original_ref"], pair["origin_actor"], pair["item_id"])
                await self._peer_memory(pair["original_ref"], reviewer, pair["item_id"])
            for record in value["read_records"]:
                if await self._record("read", record["id"], revision=1) != record:
                    raise self._broken()
            for record in value["context_records"]:
                if await self._record("context_update", record["id"], revision=1) != record:
                    raise self._broken()
            for record in value["provenance_records"]:
                if await self._record("provenance", record["id"], revision=1) != record:
                    raise self._broken()
        finally:
            _VALIDATING.reset(token)

    @asynccontextmanager
    async def _peer_input_scope(self, values, *, force=False):
        async with self._peer_record_scope():
            async with self._peer_input_scope_impl(values, force=force):
                yield

    @asynccontextmanager
    async def _peer_record_scope(self):
        if _RECORD_CACHE.get() is not None:
            yield
            return
        token = _RECORD_CACHE.set({})
        try:
            yield
        finally:
            _RECORD_CACHE.reset(token)

    @asynccontextmanager
    async def _peer_input_scope_impl(self, values, *, force=False):
        expected = {}
        for value in values:
            expected[("semantic_input", value["id"])] = value
            for kind, field in (
                ("read", "read_records"),
                ("context_update", "context_records"),
                ("provenance", "provenance_records"),
            ):
                for record in value[field]:
                    identity = (kind, record["id"])
                    if identity in expected and expected[identity] != record:
                        raise self._broken()
                    expected[identity] = record
        cache = _RECORD_CACHE.get()
        missing = {
            identity: record
            for identity, record in expected.items()
            if (self.owner_key, self.workspace_key, *identity, 1) not in cache
        }
        if missing:
            # One sealed journal snapshot verifies the complete immutable graph.
            # Current heads, Memory and Artifact grants still use fresh reads.
            observed = await self._read(
                lambda tx: (
                    {identity: tx.get(identity[0], identity[1], revision=1) for identity in missing}
                    if tx is not None
                    else None
                )
            )
            if observed != missing:
                raise self._broken()
            for identity, record in observed.items():
                cache[(self.owner_key, self.workspace_key, *identity, 1)] = record
        for identity, record in expected.items():
            if cache[(self.owner_key, self.workspace_key, *identity, 1)] != record:
                raise self._broken()
        token = _PEER.set((*_PEER.get(), *values))
        reads = {canonical(r): r for value in values for r in value["read_records"]}
        contexts = {canonical(r): r for value in values for r in value["context_records"]}
        try:
            async with self._context_archive_scope(reads.values(), contexts.values()):
                for value in values:
                    exact = (self.workspace_key, value["id"], digest(value))
                    if force or exact not in _AUTHORIZED_INPUTS.get():
                        await self._authorize_peer_input(value)
                authorized = _AUTHORIZED_INPUTS.set(
                    _AUTHORIZED_INPUTS.get()
                    | {(self.workspace_key, value["id"], digest(value)) for value in values}
                )
                try:
                    yield
                finally:
                    _AUTHORIZED_INPUTS.reset(authorized)
        finally:
            _PEER.reset(token)

    @asynccontextmanager
    async def _peer_history_scope(self, value):
        """Interpret exact old human-facing evidence, never waive live authority.

        Only this already persisted input's freshness comparison is suppressed.
        Artifact brokers and all domain authorization hooks still run normally.
        This scope is not used for model material or execution admission.
        """
        if await self._record("semantic_input", value["id"], revision=1) != value:
            raise self._broken()
        for kind, field in (
            ("read", "read_records"),
            ("context_update", "context_records"),
            ("provenance", "provenance_records"),
        ):
            for record in value[field]:
                if await self._record(kind, record["id"], revision=1) != record:
                    raise self._broken()
        key = (self.workspace_key, value["id"])
        token = _VALIDATING.set(_VALIDATING.get() | {key})
        try:
            async with self._peer_input_scope([value]):
                yield
        finally:
            _VALIDATING.reset(token)

    @asynccontextmanager
    async def peer_artifact_scope(self, snapshot, actor):
        key = (snapshot["artifact_id"], snapshot["version"], actor)
        if key in _SCOPED.get():
            yield
            return
        stack, seen, inputs = [snapshot], set(), {}
        while stack:
            value = stack.pop()
            identity = (value["artifact_id"], value["version"])
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 4096:
                raise PersonalAgentError("input_limit", "复核证据超过校验容量")
            for raw in value.get("evidence", {}).get("research_peer_read_refs", []):
                proof = normalize_peer_ref(raw)
                record = await self._record("peer_read", proof["read_id"], revision=1)
                if peer_ref(record) != proof or record["body_sha256"] != value["sha256"]:
                    raise self._broken()
                source = await self._record("semantic_input", record["input_id"], revision=1)
                if digest(source) != record["input_digest"] or actor not in {
                    source["actor_id"],
                    source["reviewer"]["id"],
                }:
                    raise PersonalAgentError("scope_denied", "这份复核证据不属于当前 Agent")
                inputs[source["id"]] = source
            for ref in value.get("source_refs", []):
                stack.append(
                    await self.artifacts._call(
                        "get",
                        actor_id=actor,
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
                )
        if not inputs:
            yield
            return
        token = _SCOPED.set(_SCOPED.get() | {key})
        try:
            async with self._peer_input_scope(list(inputs.values())):
                yield
        finally:
            _SCOPED.reset(token)

    async def prepare_research_evidence(self, evidence):
        from naumi_agent.personal.research_provenance import provenance_ref

        value, records = dict(evidence), {}
        for raw in [
            *value.get("research_read_refs", []),
            *value.get("research_egress_read_refs", []),
        ]:
            record = await self._record("read", raw["read_id"], revision=1)
            for origin in record.get("authoring_provenance_records", []):
                if origin.get("memory_refs"):
                    proof = provenance_ref(origin)
                    records[canonical(proof)] = proof
        for proof in value.get("research_authoring_provenance_refs", []):
            records[canonical(proof)] = proof
        if records:
            value["research_authoring_provenance_refs"] = list(records.values())
        return value
