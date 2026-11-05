"""Persistent evidence notebooks with real captures, exact quotes and mechanical checks."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from naumi_agent.personal.artifact_models import is_text_artifact, strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, checked_path, digest
from naumi_agent.personal.research_authority import ResearchAuthority, read_ref
from naumi_agent.personal.research_models import CreateNotebook, ReviseNotebook, validate_payload
from naumi_agent.personal.research_review import review_notebook
from naumi_agent.personal.research_store import ResearchStore

_ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_NOTEBOOK = re.compile(r"^rq_[a-f0-9]{32}$")
_SOURCE = re.compile(r"^rs_[a-f0-9]{32}$")
_CHANNELS = {"web", "cli", "tui"}
MAX_SOURCES = 64


class PersonalResearchService(ResearchAuthority):
    def __init__(self, engine, *, store=None, clock=None, capture=None, discover=None):
        self.engine, self.personal, self.artifacts = (
            engine,
            engine.personal_agents,
            engine.personal_artifacts,
        )
        self.workspace_key = self.personal.workspace_key
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode()
        ).hexdigest()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.boundary = (
            Path(engine.config.memory.session_db_path)
            .resolve()
            .with_name(f".personal-research-{self.workspace_key}-v1")
        )
        pinned = self._external_identity()
        self.store = store or ResearchStore(
            self.personal.store.base_dir / "research",
            owner_key=self.owner_key,
            expected_instance_id=pinned,
        )
        if pinned:
            if self.store.expected_instance_id not in {None, pinned}:
                raise self._broken()
            self.store.expected_instance_id = pinned
        from naumi_agent.personal.research_capture import capture_url
        from naumi_agent.personal.research_capture import discover as search

        self.capture_transport = capture or capture_url
        self.discovery_transport = discover or search

    @staticmethod
    def _broken():
        return PersonalAgentError("storage_error", "研究与原资料库不一致，请恢复原目录后继续")

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise self._broken()
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def _external_identity(self):
        if not self.boundary.exists() and not self.boundary.is_symlink():
            return None
        try:
            if (
                self.boundary.is_symlink()
                or self.boundary.stat().st_nlink != 1
                or self.boundary.stat().st_size > 1024
            ):
                raise ValueError()
            value = strict_json_loads(self.boundary.read_bytes())
            if (
                set(value) != {"version", "owner_key", "workspace_key", "instance_id"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["owner_key"] != self.owner_key
                or value["workspace_key"] != self.workspace_key
                or not re.fullmatch(r"[a-f0-9]{32}", value["instance_id"])
            ):
                raise ValueError()
            return value["instance_id"]
        except (OSError, TypeError, ValueError, PersonalAgentError):
            raise self._broken() from None

    def _guard(self):
        try:
            checked_path(self.boundary, self.store.spec)
            pinned = self._external_identity()
            if pinned:
                if not self.personal.store.db_path.is_file():
                    raise self._broken()
                self.store.expected_instance_id = pinned
            identity = self.store.identity()
            if pinned and identity != pinned:
                raise self._broken()
            if identity:
                value = {
                    "version": 1,
                    "owner_key": self.owner_key,
                    "workspace_key": self.workspace_key,
                    "instance_id": identity,
                }
                if not self.boundary.exists():
                    self.boundary.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with self.boundary.open("xb") as stream:
                            stream.write((canonical(value) + "\n").encode())
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        pass
                if self._external_identity() != identity:
                    raise self._broken()
                self.store.expected_instance_id = identity
        except (OSError, ValueError, TypeError):
            raise self._broken() from None

    async def _read(self, callback):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.read, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _write(self, callback):
        await asyncio.to_thread(self.personal.store.list)
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.write, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _record(self, kind, identity, *, revision=None, missing=False):
        def read(tx):
            if tx is None:
                if missing:
                    return None
                raise PersonalAgentError("not_found", "原研究记录不存在")
            return tx.get(kind, identity, revision=revision, missing=missing)

        return await self._read(read)

    def _actor(self, caller_actor=None):
        actor = caller_actor or "main"
        if type(actor) is not str or not _ACTOR.fullmatch(actor):
            raise PersonalAgentError("scope_denied", "研究 Agent 身份不正确")
        if actor != "main":
            self.personal.require_active_profile(actor)
        return actor

    async def _notebook(self, identity, actor, *, revision=None):
        self._actor(actor)
        if not isinstance(identity, str) or not _NOTEBOOK.fullmatch(identity):
            raise PersonalAgentError("invalid_input", "研究编号格式不正确")
        value = await self._record("notebook", identity, revision=revision)
        if value["actor_id"] != actor or value["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原研究记录不存在")
        return value

    async def _source_records(self, notebook_id):
        return await self._read(
            lambda tx: (
                [] if tx is None else tx.list("source", parent=notebook_id, limit=MAX_SOURCES + 1)
            )
        )

    def _expired(self, source):
        try:
            return self.now() >= datetime.fromisoformat(source["expires_at"])
        except (TypeError, ValueError, KeyError):
            raise self._broken() from None

    async def _body(self, source, actor):
        if source["actor_id"] != actor or source["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原研究来源不存在")
        ref = source.get("artifact_ref")
        if not ref:
            return None
        await self.artifacts.authorize_refs(actor_id=actor, refs=[ref])
        snapshot, content = await self.artifacts.read(
            actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
        )
        if snapshot["sha256"] != source["body_sha256"] or not is_text_artifact(snapshot):
            raise self._broken()
        try:
            body = content.decode("utf-8")
        except UnicodeError:
            raise self._broken() from None
        if len(body) > 120000:
            raise self._broken()
        return body

    async def _context_refs(self, actor, from_model, notebook_id=None):
        session = getattr(self.engine, "_session", None)
        if not from_model or session is None:
            return []
        if await self.artifacts.session_actor(session.id) != actor:
            raise PersonalAgentError("binding_conflict", "研究与当前对话身份不一致")
        result = []
        for ref in await self.artifacts.session_refs(session.id, actor):
            snapshot, content = await self.artifacts.read(
                actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            proofs = snapshot.get("evidence", {}).get("research_read_refs", [])
            retained, self_read = [], False
            for proof in proofs:
                record = await self._record("read", proof["read_id"], revision=1)
                if record["notebook_id"] == notebook_id and record["actor_id"] == actor:
                    self_read = True
                else:
                    retained.append(proof)
            if not self_read:
                result.append(ref)
                continue
            # Editing the exact notebook consumes its previous state by CAS. Do
            # not create a self-invalidating dependency on that mutable head.
            # Preserve the exact original bytes, all upstream ACLs and every
            # non-self research proof in a distinct, service-only context copy.
            mirrored = await self.artifacts.publish(
                actor_id=actor,
                request_id="research-context-" + digest({"id": notebook_id, "ref": ref})[:48],
                title="研究原修订上下文",
                kind="material",
                filename="research-context.json",
                content=content,
                source_refs=snapshot["source_refs"],
                evidence={
                    "research_read_refs": retained,
                    "source_kind": "research_revision_context",
                    "semantic_verification": "not_performed",
                },
            )
            result.append(self.artifacts.reference(mirrored))
        return result

    async def _inputs(self, notebook, actor):
        await self.artifacts.authorize_refs(actor_id=actor, refs=notebook.get("context_refs", []))
        records = await self._source_records(notebook["id"])
        if len(records) > MAX_SOURCES:
            raise self._broken()
        sources = {}
        referenced = {}
        for claim in notebook["claims"]:
            for citation in claim["citations"]:
                referenced.setdefault(citation["source_id"], set()).add(citation["source_revision"])
        for current in records:
            versions = referenced.pop(current["id"], {current["revision"]})
            versions.add(current["revision"])
            revisions = {}
            for revision in versions:
                historical = (
                    current
                    if revision == current["revision"]
                    else await self._record("source", current["id"], revision=revision)
                )
                revisions[str(revision)] = {
                    "revision": revision,
                    "body": await self._body(historical, actor),
                    "status": current["status"],
                    "latest_revision": current["revision"],
                    "digest": historical["body_sha256"],
                    "current_digest": current["body_sha256"],
                    "expires_at": current["expires_at"],
                    "upstream_unverified": historical["upstream_unverified"],
                }
            sources[current["id"]] = {**revisions[str(current["revision"])], "revisions": revisions}
        if referenced:
            raise PersonalAgentError("invalid_input", "引用来源不属于本次研究")
        upstream = {}
        for claim in notebook["claims"]:
            for ref in claim["upstream_claims"]:
                parent = await self._notebook(ref["notebook_id"], actor, revision=ref["revision"])
                if parent["id"] == notebook["id"]:
                    raise PersonalAgentError("invalid_input", "研究不能循环引用自身")
                matches = [value for value in parent["claims"] if value["id"] == ref["claim_id"]]
                if not matches:
                    raise PersonalAgentError("invalid_input", "上游结论不存在")
                # Only exact authorized revisions may contribute downstream labels.
                for source in await self._source_records(parent["id"]):
                    await self._body(source, actor)
                upstream[(ref["notebook_id"], ref["revision"], ref["claim_id"])] = {
                    "available": True,
                    "kind": matches[0]["kind"],
                    "verification": "unverified",
                    "upstream_unverified": True,
                }
        return records, sources, upstream

    def _observed_sources(self, records, basis):
        observed = {value["id"]: value for value in records}
        for ref in basis["sources"]:
            original = observed.get(ref["id"])
            if original is not None and (
                ref["latest_digest"] != digest(original)
                or ref["expired"] != self._expired(original)
            ):
                raise PersonalAgentError("research_busy", "来源在检查期间已变化，请重新读取原研究")
        if not set(observed).issubset({ref["id"] for ref in basis["sources"]}):
            raise PersonalAgentError("research_busy", "来源在检查期间已变化，请重新读取原研究")

    async def _view(self, notebook, actor):
        try:
            records, sources, upstream = await self._inputs(notebook, actor)
            checked = review_notebook(notebook, sources, upstream, self.now())
            basis = (await self._basis(notebook, require_current=False))[0]
            self._observed_sources(records, basis)
            basis_digest = digest(basis)
            checked["digest"] = digest(
                {
                    "notebook": digest(notebook),
                    "basis": basis_digest,
                    "review": {key: value for key, value in checked.items() if key != "checked_at"},
                }
            )

            def matching_review(tx):
                after = None
                while tx is not None:
                    rows = tx.list("review", parent=notebook["id"], limit=256, after=after)
                    for value in rows:
                        if (
                            value["notebook_revision"] == notebook["revision"]
                            and value["basis_digest"] == basis_digest
                            and value["review_digest"] == checked["digest"]
                        ):
                            return value
                    if len(rows) < 256:
                        break
                    after = rows[-1]["id"]
                return None

            exact = await self._read(matching_review)
            if exact:
                checked["id"] = exact["id"]
            if not exact:
                checked["status"] = "not_reviewed"
            summaries = [
                {
                    key: value.get(key)
                    for key in (
                        "id",
                        "revision",
                        "title",
                        "origin",
                        "url",
                        "captured_at",
                        "expires_at",
                        "status",
                        "body_sha256",
                        "artifact_ref",
                        "upstream_unverified",
                        "truncated",
                    )
                }
                | {"expired": self._expired(value)}
                for value in records
            ]
            publication = await self._record(
                "publication", "rpub_" + notebook["id"][3:], missing=True
            )
            return {
                "notebook": notebook,
                "sources": summaries,
                "review": checked,
                "publication": None if publication is None else publication["artifact_ref"],
                "availability": "available",
                "snapshot_at": self.stamp(),
                "snapshot_semantics": "exact_revision",
                "historical": notebook["revision"]
                != (await self._notebook(notebook["id"], actor))["revision"],
            }
        except PersonalAgentError as exc:
            if exc.code in {"invalid_input", "storage_error", "research_busy"}:
                raise
            return {
                "notebook": {
                    **notebook,
                    "title": "来源不可用",
                    "question": "",
                    "claims": [],
                    "conclusion": {"stance": "undecided", "premises": [], "invalidators": []},
                },
                "sources": [],
                "review": {
                    "status": "needs_sources",
                    "findings": [],
                    "anchors": [],
                    "semantic_verification": "not_performed",
                },
                "publication": None,
                "availability": "source_unavailable",
                "historical": False,
            }

    async def context(self, *, caller_actor=None):
        return {
            "actor_id": self._actor(caller_actor),
            "workspace_key": self.workspace_key,
            "server_now": self.stamp(),
        }

    async def get(self, id, revision=None, *, caller_actor=None):
        actor = self._actor(caller_actor)
        return await self._view(await self._notebook(id, actor, revision=revision), actor)

    async def list(self, limit=20, after=None, *, caller_actor=None):
        actor = self._actor(caller_actor)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise PersonalAgentError("invalid_input", "研究分页数量不正确")
        if after is not None and (type(after) is not str or not _NOTEBOOK.fullmatch(after)):
            raise PersonalAgentError("invalid_input", "研究续读位置不正确")

        def visible_page(tx):
            values, cursor = [], after
            while tx is not None:
                rows = tx.list("notebook", limit=256, after=cursor)
                for value in rows:
                    if value["actor_id"] == actor and value["workspace_key"] == self.workspace_key:
                        values.append(value)
                        if len(values) > limit:
                            return values
                if len(rows) < 256:
                    break
                cursor = rows[-1]["id"]
            return values

        values = await self._read(visible_page)
        result = []
        for value in values[:limit]:
            view = await self._view(value, actor)
            result.append(
                {
                    key: view["notebook"][key]
                    for key in ("id", "revision", "title", "question", "domain", "updated_at")
                }
                | {
                    "claim_count": len(view["notebook"]["claims"]),
                    "source_count": len(view["sources"]),
                    "review_status": view["review"]["status"],
                }
            )
        return {
            "notebooks": result,
            "next_after": result[-1]["id"] if len(values) > limit else None,
        }

    async def source(self, id, revision=None, *, caller_actor=None):
        actor = self._actor(caller_actor)
        if not isinstance(id, str) or not _SOURCE.fullmatch(id):
            raise PersonalAgentError("invalid_input", "来源编号格式不正确")
        record = await self._record("source", id, revision=revision)
        await self._notebook(record["notebook_id"], actor)
        try:
            body = await self._body(record, actor)
            return {"source": record, "body": body, "availability": "available"}
        except PersonalAgentError:
            return {
                "source": {
                    "id": id,
                    "revision": record["revision"],
                    "title": "来源不可用",
                    "status": "unavailable",
                },
                "body": None,
                "availability": "source_unavailable",
            }

    async def history(self, id, limit=20, after_revision=None, *, caller_actor=None):
        actor = self._actor(caller_actor)
        current = await self._notebook(id, actor)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise PersonalAgentError("invalid_input", "历史分页数量不正确")
        if after_revision is not None and (
            type(after_revision) is not int or not 1 <= after_revision <= 2**53 - 1
        ):
            raise PersonalAgentError("invalid_input", "研究历史续读位置不正确")
        high = (
            current["revision"]
            if after_revision is None
            else min(after_revision - 1, current["revision"])
        )
        rows = []
        for revision in range(high, max(0, high - limit), -1):
            value = await self.get(id, revision, caller_actor=actor)
            rows.append(
                {key: value["notebook"][key] for key in ("revision", "title", "updated_at")}
                | {"claim_count": len(value["notebook"]["claims"])}
            )
        return {
            "history": rows,
            "next_after_revision": rows[-1]["revision"] if high > limit else None,
        }

    def _request_identity(self, actor, request_id):
        if type(request_id) is not str or not _REQUEST.fullmatch(request_id):
            raise PersonalAgentError("invalid_input", "研究请求编号格式不正确")
        return (
            "ri_"
            + hashlib.sha256((self.workspace_key + actor + ":" + request_id).encode()).hexdigest()[
                :32
            ]
        )

    async def _begin(self, operation, arguments, actor, *, notebook_id=""):
        identity = self._request_identity(actor, arguments["request_id"])
        fingerprint = digest({"operation": operation, "actor_id": actor, "arguments": arguments})
        if len(canonical(arguments).encode()) > 512 * 1024:
            raise PersonalAgentError("input_limit", "研究请求超过单次容量")

        def begin(tx):
            current = tx.get("intent", identity, missing=True)
            if current:
                if (
                    current["fingerprint"] != fingerprint
                    or current["operation"] != operation
                    or current["actor_id"] != actor
                    or current["workspace_key"] != self.workspace_key
                ):
                    raise PersonalAgentError("idempotency_conflict", "原研究请求已绑定不同内容")
                return current
            value = {
                "id": identity,
                "revision": 1,
                "notebook_id": notebook_id,
                "workspace_key": self.workspace_key,
                "actor_id": actor,
                "status": "pending",
                "phase": "reserved",
                "operation": operation,
                "fingerprint": fingerprint,
                "arguments": arguments,
                "created_at": self.stamp(),
                "stage": None,
                "result": None,
            }
            tx.put("intent", value, expected_revision=0)
            return value

        return await self._write(begin)

    async def _stage(self, intent, phase, stage):
        def update(tx):
            current = tx.get("intent", intent["id"])
            if current["status"] == "completed":
                return current
            if current["revision"] != intent["revision"]:
                raise PersonalAgentError("request_pending", "原研究请求正在处理，请核对原请求")
            value = {**current, "revision": current["revision"] + 1, "phase": phase, "stage": stage}
            tx.put("intent", value, expected_revision=current["revision"])
            return value

        return await self._write(update)

    async def _complete(self, intent, callback):
        def complete(tx):
            current = tx.get("intent", intent["id"])
            if current["status"] == "completed":
                return current["result"]
            if current["fingerprint"] != intent["fingerprint"]:
                raise self._broken()
            result = callback(tx)
            value = {
                **current,
                "revision": current["revision"] + 1,
                "status": "completed",
                "phase": "completed",
                "result": result,
            }
            tx.put("intent", value, expected_revision=current["revision"])
            tx.save_request(current["id"], current["operation"], current["fingerprint"], result)
            return result

        return await self._write(complete)

    async def _result(self, receipt, actor):
        view = await self.get(
            receipt["notebook_id"], receipt.get("notebook_revision"), caller_actor=actor
        )
        if receipt.get("artifact_ref") and view["availability"] == "available":
            ref = receipt["artifact_ref"]
            await self.artifacts.authorize_refs(actor_id=actor, refs=[ref])
            snapshot = await self.artifacts.get(
                actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            if snapshot["sha256"] != ref["sha256"]:
                raise self._broken()
            view["publication"] = ref
        if receipt.get("discovery_id"):
            discovery = await self._record("discovery", receipt["discovery_id"])
            if discovery["actor_id"] != actor or discovery["workspace_key"] != self.workspace_key:
                raise PersonalAgentError("not_found", "原检索请求不存在")
            return {"discovery": discovery, "view": view}
        return view

    async def request(self, request_id, *, caller_actor=None):
        actor = self._actor(caller_actor)
        value = await self._record(
            "intent", self._request_identity(actor, request_id), missing=True
        )
        if value is None:
            return {"status": "unknown", "request_id": request_id}
        if value["actor_id"] != actor or value["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原研究请求不存在")
        if value["status"] == "completed":
            result = await self._result(value["result"], actor)
            return {
                "status": "completed",
                "request_id": request_id,
                "view": result.get("view", result),
            }
        return {
            "status": "unknown" if value["phase"] == "started" else "pending",
            "request_id": request_id,
        }

    async def create(self, arguments, *, caller_actor=None, _model=False):
        actor = self._actor(caller_actor)
        arguments = validate_payload(CreateNotebook, arguments)
        identity = "rq_" + self._request_identity(actor, arguments["request_id"])[3:]
        intent = await self._begin("create", arguments, actor, notebook_id=identity)
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        stamp = intent["created_at"]
        notebook = {
            "id": identity,
            "revision": 1,
            "notebook_id": identity,
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "status": "active",
            "title": arguments["title"],
            "question": arguments["question"],
            "domain": arguments["domain"],
            "claims": [],
            "conclusion": {"stance": "undecided", "premises": [], "invalidators": []},
            "created_at": stamp,
            "updated_at": stamp,
        }
        notebook["context_refs"] = await self._context_refs(actor, _model, identity)

        def write(tx):
            tx.put("notebook", notebook, expected_revision=0)
            return {"notebook_id": identity, "notebook_revision": 1}

        return await self._result(await self._complete(intent, write), actor)

    async def revise(self, arguments, *, caller_actor=None, _model=False):
        actor = self._actor(caller_actor)
        identity = arguments["id"]
        fields = validate_payload(
            ReviseNotebook, {key: value for key, value in arguments.items() if key != "id"}
        )
        normalized = {"id": identity, **fields}
        original = await self._notebook(identity, actor)
        intent = await self._begin("revise", normalized, actor, notebook_id=identity)
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        if original["revision"] != fields["expected_revision"]:
            raise PersonalAgentError("version_conflict", "研究已更新，请核对原版本后再保存")
        value = {
            **original,
            **{key: fields[key] for key in ("title", "question", "domain", "claims", "conclusion")},
            "revision": original["revision"] + 1,
            "updated_at": intent["created_at"],
        }
        refs = {
            canonical(ref): ref
            for ref in [
                *original.get("context_refs", []),
                *await self._context_refs(actor, _model, identity),
            ]
        }
        if len(refs) > 64:
            raise PersonalAgentError("input_limit", "研究上下文超过 64 个确切版本")
        value["context_refs"] = list(refs.values())
        await self._inputs(value, actor)

        def write(tx):
            tx.put("notebook", value, expected_revision=original["revision"])
            return {"notebook_id": identity, "notebook_revision": value["revision"]}

        return await self._result(await self._complete(intent, write), actor)

    async def _network_allowed(self, actor, trusted_channel):
        if trusted_channel in _CHANNELS:
            return
        context = getattr(self.engine, "_personal_item_context", None)
        session = getattr(self.engine, "_session", None)
        refs = await self.artifacts.session_refs(session.id, actor) if session else []
        if refs or context is not None and context.force_private:
            raise PersonalAgentError(
                "artifact_network_denied", "私有资料上下文不能外发新的检索词或网址"
            )

    async def _capture(self, operation, arguments, actor, trusted_channel):
        if operation == "recheck":
            source = await self._record("source", arguments["source_id"])
            notebook = await self._notebook(source["notebook_id"], actor)
            if source["origin"] != "url":
                raise PersonalAgentError(
                    "invalid_input", "这条来源不是网页，请导入新的确切资料版本"
                )
            target = source["id"]
        else:
            notebook = await self._notebook(arguments["id"], actor)
            source = None
            target = "rs_" + self._request_identity(actor, arguments["request_id"])[3:]
        intent = await self._begin(operation, arguments, actor, notebook_id=notebook["id"])
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        if source and source["revision"] != arguments["expected_revision"]:
            raise PersonalAgentError("version_conflict", "来源已更新，请核对原版本")
        if not source and len(await self._source_records(notebook["id"])) >= MAX_SOURCES:
            raise PersonalAgentError("input_limit", "每份研究最多保存 64 条来源")
        if intent["phase"] == "reserved":
            if operation != "import":
                await self._network_allowed(actor, trusted_channel)
            intent = await self._stage(intent, "started", None)
            try:
                if operation == "import":
                    ref = arguments["artifact_ref"]
                    await self.artifacts.authorize_refs(actor_id=actor, refs=[ref])
                    snapshot, raw = await self.artifacts.read(
                        actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
                    )
                    if not is_text_artifact(snapshot) or len(raw) > 1024 * 1024:
                        raise PersonalAgentError(
                            "invalid_input", "研究来源需要不超过 1 MB 的文本资料"
                        )
                    body = raw.decode("utf-8-sig")
                    if len(body) > 120000:
                        raise PersonalAgentError("input_limit", "来源正文超过 120000 字符")
                    stage = {
                        "body": body,
                        "title": snapshot["title"],
                        "origin": "artifact",
                        "url": None,
                        "captured_at": self.stamp(),
                        "truncated": False,
                        "raw_sha256": snapshot["sha256"],
                        "input_ref": ref,
                        "http_status": None,
                        "extractor": "provided-text-v1",
                    }
                else:
                    capture = await self.capture_transport(
                        source["url"] if source else arguments["url"]
                    )
                    stage = {
                        **capture,
                        "origin": "url",
                        "url": capture["requested_url"],
                        "input_ref": None,
                    }
            except PersonalAgentError as exc:
                if operation == "import" or exc.code == "invalid_input":
                    raise
                stage = {
                    "body": None,
                    "status": "unavailable",
                    "failure_code": exc.code,
                    "captured_at": self.stamp(),
                    "title": "来源不可用",
                    "origin": "url",
                    "url": source["url"] if source else arguments["url"],
                    "http_status": None,
                    "truncated": False,
                    "extractor": None,
                    "raw_sha256": None,
                }
            except UnicodeError:
                raise PersonalAgentError("invalid_input", "研究来源需要有效 UTF-8 文本") from None
            intent = await self._stage(intent, "captured", stage)
        if intent["phase"] != "captured":
            raise PersonalAgentError("request_pending", "原抓取结果待核对，未重复请求外部来源")
        stage = intent["stage"]
        if stage["body"] is not None:
            self._actor(actor)
            refs = [stage["input_ref"]] if stage.get("input_ref") else []
            refs.extend(await self._context_refs(actor, trusted_channel is None, notebook["id"]))
            refs = list({canonical(ref): ref for ref in refs}.values())
            material = await self.artifacts.publish(
                actor_id=actor,
                request_id="research-source-" + intent["id"],
                title=stage["title"][:160] or "研究来源",
                kind="material",
                filename="research-source.txt",
                content=stage["body"].encode(),
                source_refs=refs,
                evidence={
                    "source_kind": "research_capture",
                    "captured_at": stage["captured_at"],
                    "origin": stage["origin"],
                    "semantic_verification": "not_performed",
                },
            )
            artifact_ref = self.artifacts.reference(material)
            await self.artifacts.authorize_refs(actor_id=actor, refs=refs)
        else:
            artifact_ref = source["artifact_ref"] if source else None
        max_age = arguments.get("max_age_days", source["max_age_days"] if source else 30)
        value = {
            **(source or {}),
            "id": target,
            "revision": 1 if source is None else source["revision"] + 1,
            "notebook_id": notebook["id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "status": stage.get("status", "available"),
            "captured_at": stage["captured_at"],
            "expires_at": (
                datetime.fromisoformat(stage["captured_at"]) + timedelta(days=max_age)
            ).isoformat(),
            "max_age_days": max_age,
            "artifact_ref": artifact_ref,
            "body_sha256": artifact_ref["sha256"] if artifact_ref else None,
            "updated_at": self.stamp(),
        }
        if stage["body"] is not None:
            value.update(
                {
                    key: stage.get(key)
                    for key in (
                        "title",
                        "origin",
                        "url",
                        "raw_sha256",
                        "http_status",
                        "truncated",
                        "extractor",
                    )
                }
            )
            value["upstream_unverified"] = stage["origin"] == "artifact"
        else:
            value["failure_code"] = stage["failure_code"]
            if source is None:
                value.update(
                    {
                        key: stage.get(key)
                        for key in (
                            "title",
                            "origin",
                            "url",
                            "raw_sha256",
                            "http_status",
                            "truncated",
                            "extractor",
                        )
                    }
                )
                value["upstream_unverified"] = True
        await self._notebook(notebook["id"], actor)

        def write(tx):
            if (
                not source
                and len(tx.list("source", parent=notebook["id"], limit=MAX_SOURCES + 1))
                >= MAX_SOURCES
            ):
                raise PersonalAgentError("input_limit", "研究来源数量已达上限")
            tx.put("source", value, expected_revision=0 if source is None else source["revision"])
            return {"notebook_id": notebook["id"], "notebook_revision": notebook["revision"]}

        return await self._result(await self._complete(intent, write), actor)

    async def search(self, arguments, *, caller_actor=None, trusted_channel=None):
        actor = self._actor(caller_actor)
        notebook = await self._notebook(arguments["id"], actor)
        intent = await self._begin("search", arguments, actor, notebook_id=notebook["id"])
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        if intent["phase"] == "reserved":
            await self._network_allowed(actor, trusted_channel)
            intent = await self._stage(intent, "started", None)
            tool = self.engine.tool_registry.get("web_search")
            if tool is None:
                raise PersonalAgentError("source_unavailable", "网络搜索尚未配置")
            result = await self.discovery_transport(
                tool, arguments["query"], arguments.get("max_results", 5)
            )
            intent = await self._stage(intent, "captured", result)
        if intent["phase"] != "captured":
            raise PersonalAgentError("request_pending", "原检索结果待核对，未重复外部检索")
        result = intent["stage"]
        discovery = {
            **result,
            "id": "rd_" + intent["id"][3:],
            "revision": 1,
            "notebook_id": notebook["id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "text": "\n\n".join(
                item["title"] + "\n" + item["url"] + "\n" + item.get("snippet", "")
                for item in result.get("items", [])
            ),
            "evidence_kind": "discovery_only",
        }

        def write(tx):
            tx.put("discovery", discovery, expected_revision=0)
            return {
                "notebook_id": notebook["id"],
                "notebook_revision": notebook["revision"],
                "discovery_id": discovery["id"],
            }

        return await self._result(await self._complete(intent, write), actor)

    async def review(self, arguments, *, caller_actor=None):
        actor = self._actor(caller_actor)
        notebook = await self._notebook(arguments["id"], actor)
        intent = await self._begin("review", arguments, actor, notebook_id=notebook["id"])
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        if arguments["expected_revision"] != notebook["revision"]:
            raise PersonalAgentError("version_conflict", "研究已更新，请复核原版本")
        records, sources, upstream = await self._inputs(notebook, actor)
        result = review_notebook(notebook, sources, upstream, self.now())
        basis, _ = await self._basis(notebook, require_current=True)
        self._observed_sources(records, basis)
        basis_digest = digest(basis)
        value = {
            "id": "rr_" + intent["id"][3:],
            "revision": 1,
            "notebook_id": notebook["id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "status": result["status"],
            "notebook_revision": notebook["revision"],
            "basis_digest": basis_digest,
            "created_at": self.stamp(),
            "result": result,
            "reviewer_kind": "deterministic_code",
            "reviewer_run_id": None,
            "review_digest": digest(
                {
                    "notebook": digest(notebook),
                    "basis": basis_digest,
                    "review": {key: item for key, item in result.items() if key != "checked_at"},
                }
            ),
        }

        def write(tx):
            if digest(tx.get("notebook", notebook["id"])) != digest(notebook):
                raise PersonalAgentError("version_conflict", "研究在复核期间已变化")
            for ref in basis["sources"]:
                latest = tx.get("source", ref["id"])
                if (
                    digest(latest) != ref["latest_digest"]
                    or self._expired(latest) != ref["expired"]
                ):
                    raise PersonalAgentError("research_busy", "来源在复核期间已变化，请重新检查")
            tx.put("review", value, expected_revision=0)
            return {"notebook_id": notebook["id"], "notebook_revision": notebook["revision"]}

        return await self._result(await self._complete(intent, write), actor)

    @staticmethod
    def _report(view):
        notebook, review = view["notebook"], view["review"]
        labels = {"fact": "事实主张·语义未核实", "estimate": "估算", "unknown": "未知"}
        lines = [
            "# " + notebook["title"],
            "",
            notebook["question"],
            "",
            f"研究 {notebook['id']} · 修订 {notebook['revision']}",
            f"机械检查：{review['status']}；语义独立核实：未执行。",
            "",
            "本报告保留捕获时刻与原版本，来源发生变化或过期后须重新读取。",
            "",
        ]
        for claim in notebook["claims"]:
            lines += [f"## {claim['id']} · {labels[claim['kind']]}", "", claim["text"], ""]
            if claim.get("quantity"):
                quantity = claim["quantity"]
                lines += [
                    f"数量：{quantity['value']} {quantity['unit']} "
                    f"{quantity.get('currency') or ''} {quantity.get('period') or ''}",
                    "",
                ]
            for assumption in claim["assumptions"]:
                lines += ["假设：" + assumption]
            for ref in claim["upstream_claims"]:
                lines += [
                    f"上游：{ref['notebook_id']} v{ref['revision']} / {ref['claim_id']}；"
                    "保留未独立核实状态。"
                ]
            for anchor in review["anchors"]:
                if anchor["claim_id"] != claim["id"]:
                    continue
                lines += [
                    "",
                    "> " + anchor["quote"].replace("\n", "\n> "),
                    f"来源：{anchor['source_id']} v{anchor['source_revision']}，"
                    f"正文位置 {anchor['start']}–{anchor['end']}，SHA256 {anchor['sha256']}",
                ]
            lines += [""]
        lines += ["## 判断", "", notebook["conclusion"]["stance"], ""]
        lines += ["前提：" + value for value in notebook["conclusion"]["premises"]]
        lines += ["失效条件：" + value for value in notebook["conclusion"]["invalidators"]]
        lines += ["", "## 检查发现", ""]
        lines += [
            f"- {value['code']} / {value['claim_id']}：{value['message']}"
            for value in review["findings"]
        ]
        lines += ["", "## 来源版本", ""]
        for source in view["sources"]:
            lines += [
                f"- {source['id']} v{source['revision']} · {source['title']} · "
                f"捕获 {source['captured_at']} · 失效 {source['expires_at']} · "
                f"状态 {source['status']} · {source.get('url') or '所提供的确切资料'}"
            ]
        return ("\n".join(lines) + "\n").encode("utf-8")

    async def publish(self, arguments, *, caller_actor=None):
        actor = self._actor(caller_actor)
        notebook = await self._notebook(arguments["id"], actor)
        intent = await self._begin("publish", arguments, actor, notebook_id=notebook["id"])
        if intent["status"] == "completed":
            return await self._result(intent["result"], actor)
        if intent["phase"] == "reserved":
            view = await self._view(notebook, actor)
            review = view["review"]
            if notebook["revision"] != arguments["expected_revision"]:
                raise PersonalAgentError("version_conflict", "研究版本已变化，请重新打开")
            if (
                view["availability"] != "available"
                or not review.get("id")
                or review.get("digest") != arguments["expected_review_digest"]
            ):
                raise PersonalAgentError("source_stale", "请先对当前确切来源执行机械检查")
            # Reports with findings remain explicit drafts, not successful truth claims.
            content = self._report(view)
            read, _ = await self._new_read(
                content, notebook, actor, require_current=False, publication=True
            )
            original_review = await self._record("review", review["id"], revision=1)
            if digest(read["basis"]) != original_review["basis_digest"]:
                raise PersonalAgentError("source_stale", "来源在报告形成期间已变化，请重新检查")
            previous = await self._record("publication", "rpub_" + notebook["id"][3:], missing=True)
            intent = await self._stage(
                intent,
                "captured",
                {
                    "read_id": read["id"],
                    "body": content.decode(),
                    "previous": previous,
                    "notebook_revision": notebook["revision"],
                    "review_digest": review["digest"],
                },
            )
        if intent["phase"] != "captured":
            raise PersonalAgentError("request_pending", "原报告发布结果待核对")
        stage = intent["stage"]
        read = await self._record("read", stage["read_id"], revision=1)
        await self._read_authority(read, actor)
        previous = stage["previous"]
        snapshot = await self.artifacts.publish(
            actor_id=actor,
            request_id="research-report-" + intent["id"],
            title=notebook["title"],
            kind="output",
            filename="research-report.md",
            content=stage["body"].encode(),
            artifact_id=previous["artifact_ref"]["artifact_id"] if previous else None,
            expected_version=previous["artifact_ref"]["version"] if previous else None,
            source_refs=read["source_refs"],
            evidence={
                "research_read_refs": [read_ref(read)],
                "source_kind": "research_report",
                "semantic_verification": "not_performed",
            },
        )
        await self._read_authority(read, actor)
        publication = {
            "id": "rpub_" + notebook["id"][3:],
            "revision": 1 if previous is None else previous["revision"] + 1,
            "notebook_id": notebook["id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "status": "published",
            "created_at": self.stamp(),
            "notebook_revision": stage["notebook_revision"],
            "review_digest": stage["review_digest"],
            "artifact_ref": self.artifacts.reference(snapshot),
        }

        def write(tx):
            if tx.get("notebook", notebook["id"])["revision"] != stage["notebook_revision"]:
                raise PersonalAgentError("source_stale", "报告形成期间研究已变化，请核对原请求")
            tx.put(
                "publication",
                publication,
                expected_revision=0 if previous is None else previous["revision"],
            )
            return {
                "notebook_id": notebook["id"],
                "notebook_revision": stage["notebook_revision"],
                "artifact_ref": publication["artifact_ref"],
            }

        return await self._result(await self._complete(intent, write), actor)

    async def execute_operation(self, arguments, *, caller_actor=None, trusted_channel=None):
        actor = self._actor(caller_actor)
        if caller_actor is None and trusted_channel not in _CHANNELS:
            raise PersonalAgentError("scope_denied", "请从已验证的用户入口管理研究")
        if caller_actor is not None and trusted_channel is not None:
            raise PersonalAgentError("scope_denied", "研究调用不能声明人工身份")
        operation = arguments["operation"]
        fields = {key: value for key, value in arguments.items() if key != "operation"}
        if operation in {"capture", "import", "recheck"}:
            return await self._capture(operation, fields, actor, trusted_channel)
        if operation == "search":
            return await self.search(fields, caller_actor=actor, trusted_channel=trusted_channel)
        if operation in {"create", "revise", "review", "publish"}:
            options = (
                {"_model": caller_actor is not None} if operation in {"create", "revise"} else {}
            )
            return await getattr(self, operation)(fields, caller_actor=actor, **options)
        if operation == "request":
            return await self.request(fields["request_id"], caller_actor=actor)
        if operation in {"context", "list", "get", "source", "history"}:
            return await getattr(self, operation)(**fields, caller_actor=actor)
        raise PersonalAgentError("invalid_input", "研究操作不存在")
