"""Explicit read-only federation over already running personal workspaces."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import stat
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import checked_path
from naumi_agent.personal.workspace_models import (
    MAX_BODY_BYTES,
    ReadRef,
    Source,
    connection_view,
    endpoint_url,
    error,
    grant_view,
    identity,
    normalize,
)
from naumi_agent.personal.workspace_persistence import WorkspacePersistence
from naumi_agent.personal.workspace_source import WorkspaceSourceMixin
from naumi_agent.personal.workspace_transport import TRACE, WorkspaceTransport
from naumi_agent.personal.workspace_vault import WorkspaceVault


class PersonalWorkspaceService(WorkspaceSourceMixin, WorkspacePersistence):
    def __init__(self, engine, *, store=None, vault=None, transport=None, clock=None):
        super().__init__(engine, store=store)
        self.engine, self.items = engine, engine.personal_items
        self.vault = vault if vault is not None else WorkspaceVault()
        self.transport = transport if transport is not None else WorkspaceTransport()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._source_semaphore = asyncio.Semaphore(4)
        self._hub_semaphore = asyncio.Semaphore(4)

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise error("storage_error")
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def _local(self, record):
        if record["workspace_key"] != self.workspace_key:
            raise error("not_found")
        return record

    @staticmethod
    def _manual(caller_actor, trusted_channel):
        if caller_actor is not None or trusted_channel not in {"web", "cli", "tui"}:
            raise error("scope_denied")

    def _actor_profile(self, actor):
        if actor == "main":
            return {"id": "main", "version": 1, "name": "主 Agent"}
        if type(actor) is not str or not re.fullmatch("pa_[a-f0-9]{32}", actor):
            raise error("scope_denied")
        return self.personal.require_active_profile(actor).to_dict()

    def _today(self):
        service = getattr(self.engine, "personal_today", None)
        if service is None:
            raise error()
        return service

    def _artifacts(self):
        return self._today()._artifacts()

    def _trace_marker(self):
        return digest({"owner": self.owner_key, "workspace": self.workspace_key})

    async def _directory_id(self):
        return await self._read(
            lambda tx: (
                None
                if tx is None
                else tx.db.execute("SELECT instance_id FROM workspace_meta").fetchone()[0]
            )
        )

    async def _prepare_route_files(self):
        # Explicit enrollment may initialize the session schema, never a session/run.
        await self.engine.session_store.list_sessions(page_size=1)

    def _file_bindings(self):
        result = {}
        for key, raw in (
            ("catalog", self.personal.store.db_path),
            ("sessions", Path(self.engine.config.memory.session_db_path)),
        ):
            try:
                path = checked_path(raw, self.store.spec)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino:
                    raise error("source_stale")
                result[key] = {"device": str(info.st_dev), "file_id": str(info.st_ino)}
            except OSError:
                raise error("source_stale") from None
        return result

    async def _recipient(self, actor, *, enroll=False):
        if enroll:
            await self.ensure()
            await self._prepare_route_files()
        profile = self._actor_profile(actor)
        instance = await self._directory_id()
        if instance is None or not self.enrolled():
            raise error("source_stale")
        return {
            "version": 1,
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "session_db_key": self.personal.session_db_key,
            "directory_instance_id": instance,
            "actor_id": actor,
            "actor_version": profile["version"],
        }

    async def _records(self, kind):
        def read(tx):
            if tx is None:
                return []
            page = tx.page(kind, parent=self.workspace_key, limit=1000)
            if page["next_after"] is not None:
                raise error("input_limit")
            return page["records"]

        return await self._read(read)

    def _fingerprint(self, operation, payload):
        return digest(
            {"workspace_key": self.workspace_key, "operation": operation, "payload": payload}
        )

    async def _replay(self, operation, payload):
        def read(tx):
            existing = None if tx is None else tx.request(payload["request_id"])
            if existing is None:
                return None
            if existing["operation"] != operation or existing["fingerprint"] != self._fingerprint(
                operation, payload
            ):
                raise error("idempotency_conflict")
            if existing["result"]["workspace_key"] != self.workspace_key:
                raise error("not_found")
            return existing["result"]

        return await self._read(read)

    def _receipt(self, payload, operation, record):
        return {
            "accepted": True,
            "request_id": payload["request_id"],
            "operation": operation,
            "workspace_key": self.workspace_key,
            "identity": {key: record[key] for key in ("id", "revision", "status")},
        }

    async def _mutate(self, operation, payload, callback, *, lease=None):
        def write(tx):
            old = tx.request(payload["request_id"])
            fingerprint = self._fingerprint(operation, payload)
            if old is not None:
                if old["operation"] != operation or old["fingerprint"] != fingerprint:
                    raise error("idempotency_conflict")
                return old["result"]
            if lease is not None:
                tx.require_lease(
                    "credential", lease["slot"], lease["holder"], lease["fence"], now=time.time()
                )
            result = callback(tx)
            tx.save_request(payload["request_id"], operation, fingerprint, result)
            return result

        return await self._write(write)

    @asynccontextmanager
    async def _slot(self, slot):
        holder = uuid.uuid4().hex
        lease = await self._write(
            lambda tx: tx.claim("credential", slot, holder, now=time.time(), ttl=60)
        )
        if lease is None:
            raise error("workspace_busy")
        try:
            yield {"slot": slot, "holder": holder, "fence": lease["fence"]}
        finally:
            try:
                await self._write(
                    lambda tx: tx.release(
                        "credential", slot, holder, lease["fence"], now=time.time()
                    )
                )
            except Exception:
                # Cleanup cannot replace an already committed receipt; the slot lease expires.
                self._last_lease_cleanup_error = "workspace_unknown"

    async def _vault_seal(self, slot, value):
        await asyncio.to_thread(self.vault.provision, owner_key=self.owner_key, slot=slot)
        return await asyncio.to_thread(
            self.vault.seal,
            owner_key=self.owner_key,
            slot=slot,
            purpose="workspace-capability",
            value=value,
        )

    async def _vault_open(self, slot, envelope):
        result = await asyncio.to_thread(
            self.vault.open,
            owner_key=self.owner_key,
            slot=slot,
            purpose="workspace-capability",
            envelope=envelope,
        )
        if (
            set(result) != {"secret"}
            or type(result["secret"]) is not str
            or not re.fullmatch("[a-f0-9]{64}", result["secret"])
        ):
            raise error("vault_corrupt")
        return result

    async def context(self, caller_actor=None):
        if caller_actor is not None:
            self._actor_profile(caller_actor)
        connections = await self._records("connection")
        grants = await self._records("grant") if caller_actor is None else []
        actors = (
            [{"id": "main", "name": "主 Agent", "version": 1}]
            + [{"id": p.id, "name": p.name, "version": p.version} for p in self.personal.list()]
            if caller_actor is None
            else []
        )
        return {
            "workspace_key": self.workspace_key,
            "owner_key": self.owner_key,
            "configured": self.enrolled(),
            "actors": actors,
            "connection_count": sum(
                c["status"] == "active"
                and (caller_actor is None or c["recipient"]["actor_id"] == caller_actor)
                for c in connections
            ),
            "grant_count": sum(g["status"] == "active" for g in grants),
        }

    async def _connection(self, connection_id, actor=None, *, active=True):
        value = await self._record("connection", connection_id)
        if active and value["status"] != "active":
            raise error("grant_revoked")
        if actor is not None and value["recipient"]["actor_id"] != actor:
            raise error("not_found")
        if value["recipient"] != await self._recipient(value["recipient"]["actor_id"]):
            raise error("source_stale")
        if value["recipient_actor_digest"] != digest(
            self._actor_profile(value["recipient"]["actor_id"])
        ):
            raise error("source_stale")
        if value["recipient_file_bindings"] != self._file_bindings():
            raise error("source_stale")
        return value

    async def list(self, caller_actor=None):
        if caller_actor is not None:
            self._actor_profile(caller_actor)
        values = await self._records("connection")
        return {
            "connections": [
                connection_view(value)
                for value in values
                if caller_actor is None or value["recipient"]["actor_id"] == caller_actor
            ]
        }

    async def connect(self, payload, *, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        await self.ensure()
        prior = await self._replay("connect", payload)
        if prior is not None:
            value = await self._record("connection", prior["identity"]["id"])
            return {**prior, "connection": connection_view(value)}
        ticket = payload["ticket"]
        recipient = await self._recipient(ticket["recipient"]["actor_id"])
        if (
            ticket["recipient"] != recipient
            or ticket["source"]["workspace_key"] == self.workspace_key
        ):
            raise error("source_stale")
        endpoint = endpoint_url(payload["endpoint"])
        label = payload["label"]
        if label != label.strip() or any(ord(c) < 32 for c in label):
            raise error("invalid_input")
        connection_id = identity(
            "wc_", {"workspace": self.workspace_key, "request": payload["request_id"]}
        )
        grant = {
            "id": ticket["grant_id"],
            "revision": ticket["grant_revision"],
            "status": "active",
            **{
                key: ticket[key]
                for key in (
                    "source",
                    "recipient",
                    "source_actor_id",
                    "source_actor_version",
                    "scopes",
                    "expires_at",
                )
            },
        }
        fields = {"operation": "inspect", "grant_id": ticket["grant_id"], "recipient": recipient}
        response = await self.transport.request(
            endpoint, ticket["secret"], fields, [self._trace_marker()]
        )
        if response.get("source") != ticket["source"] or type(response.get("grant")) is not dict:
            raise error("source_stale")
        if {k: response["grant"].get(k) for k in grant} != grant:
            raise error("source_stale")
        grant = response["grant"]
        value = {
            "id": connection_id,
            "workspace_key": self.workspace_key,
            "status": "active",
            "label": label,
            "endpoint": endpoint,
            "grant_id": ticket["grant_id"],
            "grant": grant,
            **{
                key: ticket[key]
                for key in (
                    "source",
                    "recipient",
                    "source_actor_id",
                    "source_actor_version",
                    "scopes",
                    "expires_at",
                )
            },
            "recipient_actor_digest": digest(self._actor_profile(recipient["actor_id"])),
            "recipient_file_bindings": self._file_bindings(),
            "created_at": self.stamp(),
        }
        async with self._slot(connection_id) as lease:
            value["envelope"] = await self._vault_seal(connection_id, {"secret": ticket["secret"]})

            def save(tx):
                existing = tx.page("connection", parent=self.workspace_key, limit=1000)
                if existing["next_after"] or len(existing["records"]) >= 1000:
                    raise error("input_limit")
                if sum(v["status"] == "active" for v in existing["records"]) >= 16:
                    raise error("input_limit")
                if any(
                    v["grant_id"] == ticket["grant_id"] and v["source"] == ticket["source"]
                    for v in existing["records"]
                ):
                    raise error("version_conflict")
                record = tx.put("connection", value, expected_revision=0)
                return self._receipt(payload, "connect", record)

            receipt = await self._mutate("connect", payload, save, lease=lease)
        try:
            value = await self._connection(connection_id)
            await self._remote(value, "inspect")
            return {**receipt, "connection": connection_view(value)}
        except Exception:
            return {**receipt, "projection_available": False, "content_transport": "requests"}

    async def disconnect(self, payload, *, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)

        def save(tx):
            value = self._local(tx.get("connection", payload["connection_id"]))
            if value["status"] != "active":
                raise error("version_conflict")
            value = tx.put(
                "connection",
                {**value, "status": "disconnected", "disconnected_at": self.stamp()},
                expected_revision=payload["expected_revision"],
            )
            return self._receipt(payload, "disconnect", value)

        return await self._mutate("disconnect", payload, save)

    async def _remote(self, connection, operation, **extra):
        actor = connection["recipient"]["actor_id"]
        current = await self._connection(connection["id"], actor)
        if current != connection:
            raise error("source_stale")
        secret = (await self._vault_open(current["id"], current["envelope"]))["secret"]
        trace = TRACE.get()
        marker = self._trace_marker()
        if marker not in trace:
            trace = (*trace, marker)
        if len(trace) >= 8:
            raise error()
        fields = {
            "operation": operation,
            "grant_id": current["grant_id"],
            "recipient": current["recipient"],
            **extra,
        }
        async with self._hub_semaphore:
            response = await self.transport.request(
                current["endpoint"], secret, fields, list(trace)
            )
        if (
            set(response) != {"source", "grant", "body", "read_ref"}
            or normalize(Source, response["source"]) != current["source"]
            or response["grant"] != current["grant"]
            or type(response["body"]) is not dict
        ):
            raise error("source_stale")
        if operation in {"page", "result"}:
            ref = normalize(ReadRef, response["read_ref"])
            if (
                hashlib.sha256(canonical(response["body"]).encode()).hexdigest()
                != ref["body_sha256"]
            ):
                raise error("source_stale")
        elif operation == "validate":
            if response["body"] != {"valid": True} or response["read_ref"] != extra["read_ref"]:
                raise error("source_stale")
        elif response["body"] != {} or response["read_ref"] is not None:
            raise error("source_stale")
        if await self._connection(connection["id"], actor) != current:
            raise error("grant_revoked")
        return response

    @staticmethod
    def _envelope(connection):
        return {
            "connection_id": connection["id"],
            "connection_revision": connection["revision"],
            "source": connection["source"],
            "recipient": connection["recipient"],
        }

    def _remote_ref(self, connection, response):
        return {**self._envelope(connection), "source_read_ref": response["read_ref"]}

    async def _overview(self, *, connection_id=None, after=None, limit=50, caller_actor=None):
        if after is not None and connection_id is None:
            raise error("invalid_input")
        if connection_id is not None:
            connections = [await self._connection(connection_id, caller_actor)]
        else:
            connections = [
                v
                for v in await self._records("connection")
                if v["status"] == "active"
                and (caller_actor is None or v["recipient"]["actor_id"] == caller_actor)
            ]

        async def query(connection):
            envelope = self._envelope(connection)
            try:
                response = await self._remote(
                    connection, "page", limit=limit, **({"after": after} if after else {})
                )
                body = response["body"]
                if (
                    set(body) != {"entries", "next_after", "partial"}
                    or type(body["entries"]) is not list
                ):
                    raise error("source_stale")
                return {
                    **envelope,
                    "label": connection["label"],
                    "availability": "available",
                    "code": None,
                    "entries": body["entries"],
                    "next_after": body["next_after"],
                    "partial": body["partial"],
                }, self._remote_ref(connection, response)
            except PersonalAgentError as exc:
                return {
                    **envelope,
                    "label": connection["label"],
                    "availability": "unavailable",
                    "code": error(exc.code).code,
                    "entries": [],
                    "next_after": None,
                    "partial": True,
                }, None

        values = await asyncio.gather(*(query(v) for v in connections))
        sources = [v[0] for v in values]
        return {
            "sources": sources,
            "partial": any(v["availability"] != "available" or v["partial"] for v in sources),
            "consistency": "live_keyset",
        }, [v[1] for v in values if v[1]]

    async def overview(self, **arguments):
        return (await self._overview(**arguments))[0]

    async def _result(self, connection_id, item_id, caller_actor=None):
        connection = await self._connection(connection_id, caller_actor)
        envelope = self._envelope(connection)
        try:
            response = await self._remote(connection, "result", item_id=item_id)
            body = response["body"]
            if set(body) != {"item", "result"} or body["item"]["item_id"] != item_id:
                raise error("source_stale")
            return {**envelope, "availability": "available", "code": None, **body}, [
                self._remote_ref(connection, response)
            ]
        except PersonalAgentError as exc:
            return {
                **envelope,
                "availability": "unavailable",
                "code": error(exc.code).code,
                "item": None,
                "result": None,
            }, []

    async def result(self, **arguments):
        return (await self._result(**arguments))[0]

    async def request_status(self, request_id):
        def read(tx):
            record = None if tx is None else tx.request(request_id)
            if record is None or record["result"].get("workspace_key") != self.workspace_key:
                return {"status": "missing", "request_id": request_id}
            return {
                "status": "accepted",
                "request_id": request_id,
                "operation": record["operation"],
                "identity": record["result"]["identity"],
            }

        return await self._read(read)

    async def execute_operation(self, arguments, *, caller_actor=None, trusted_channel=None):
        from naumi_agent.personal.workspace_tools import validate_workspace_arguments

        fields = validate_workspace_arguments(arguments, human=caller_actor is None)
        operation = fields.pop("operation")
        if caller_actor is not None:
            self._actor_profile(caller_actor)
        if operation in {
            "issue",
            "revoke",
            "connect",
            "disconnect",
            "recipient",
            "grants",
            "requests",
        }:
            if caller_actor is not None or trusted_channel not in {"web", "cli", "tui"}:
                raise error("scope_denied")
        if operation in {"issue", "revoke", "connect", "disconnect"}:
            return await getattr(self, operation)(
                fields["payload"], caller_actor=caller_actor, trusted_channel=trusted_channel
            )
        if operation == "recipient":
            return {"recipient": await self._recipient(fields["actor_id"], enroll=True)}
        if operation == "grants":
            return {"grants": [grant_view(v) for v in await self._records("grant")]}
        if operation == "requests":
            return await self.request_status(fields["request_id"])
        return await getattr(self, operation)(**fields, caller_actor=caller_actor)

    async def _read_authority(self, record, actor):
        if record["workspace_key"] != self.workspace_key or record["actor_id"] != actor:
            raise error()
        if record["actor_digest"] != digest(self._actor_profile(actor)):
            raise error("source_stale")
        for ref in record["source_refs"]:
            connection = await self._connection(ref["connection_id"], actor)
            if self._envelope(connection) != {k: ref[k] for k in self._envelope(connection)}:
                raise error("source_stale")
            if ref["source_read_ref"] is None:
                await self._remote(connection, "inspect")
            else:
                await self._remote(connection, "validate", read_ref=ref["source_read_ref"])

    async def _pin_body(self, body, actor, refs):
        body = copy.deepcopy(body)
        content = canonical(body).encode()
        if len(content) > MAX_BODY_BYTES:
            raise error("input_limit")
        session, version = await self._today()._read_session(actor)
        record = {
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "actor_digest": digest(self._actor_profile(actor)),
            "body_sha256": hashlib.sha256(content).hexdigest(),
            "source_refs": copy.deepcopy(refs),
        }
        record["id"] = identity("wx_", record)
        await self._read_authority(record, actor)

        def save(tx):
            return tx.get("read", record["id"], missing=True) or tx.put(
                "read", record, expected_revision=0
            )

        saved = await self._write(save)
        snapshot = await self._artifacts().publish(
            actor_id=actor,
            request_id="workspace-read:" + digest(record),
            title="全部事项",
            kind="material",
            filename="workspaces.txt",
            content=content,
            source_refs=[],
            evidence={
                "source_kind": "workspace_read",
                "workspace_read_refs": [
                    {
                        "read_id": saved["id"],
                        "revision": 1,
                        "digest": digest(saved),
                        "workspace_key": self.workspace_key,
                    }
                ],
            },
        )
        if snapshot["sha256"] != record["body_sha256"]:
            raise error("storage_error")
        await self._artifacts().bind_refs(
            session.id, actor, [self._artifacts().reference(snapshot)]
        )
        if (await self._today()._read_session(actor, session))[1] != version:
            raise error("source_stale")
        await self._read_authority(saved, actor)
        return body

    async def model_result(self, operation, result, actor, arguments):
        # Never trust the caller's detached DTO as a source manifest.
        fields = {key: value for key, value in arguments.items() if key != "operation"}
        if operation == "overview":
            body, refs = await self._overview(**fields, caller_actor=actor)
        elif operation == "result":
            body, refs = await self._result(**fields, caller_actor=actor)
        elif operation == "list":
            values = (await self.list(caller_actor=actor))["connections"]
            body, refs = {"connections": []}, []
            for value in values:
                if value["status"] != "active":
                    continue
                try:
                    connection = await self._connection(value["id"], actor)
                    response = await self._remote(connection, "inspect")
                except PersonalAgentError:
                    continue
                body["connections"].append(
                    {
                        k: value[k]
                        for k in ("id", "revision", "label", "status", "scopes", "expires_at")
                    }
                )
                refs.append(self._remote_ref(connection, response))
        else:
            body = await self.context(caller_actor=actor)
            body.pop("actors", None)
            body.pop("grant_count", None)
            refs = []
        if not self.enrolled():
            # No private source bodies exist before explicit enrollment.
            return body
        return await self._pin_body(body, actor, refs)

    async def authorize_artifact(self, snapshot, actor_id):
        stack, seen = [snapshot], set()
        while stack:
            value = stack.pop()
            key = (value["artifact_id"], value["version"], value["sha256"], actor_id)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 4096:
                raise error("input_limit")
            for raw in value.get("evidence", {}).get("workspace_read_refs", []):
                if (
                    type(raw) is not dict
                    or set(raw) != {"read_id", "revision", "digest", "workspace_key"}
                    or raw["workspace_key"] != self.workspace_key
                    or type(raw["revision"]) is not int
                    or raw["revision"] != 1
                ):
                    raise error()
                record = await self._record("read", raw["read_id"], revision=1)
                if digest(record) != raw["digest"] or value["sha256"] != record["body_sha256"]:
                    raise error()
                await self._read_authority(record, actor_id)
            for ref in value.get("source_refs", []):
                parent = await self._artifacts()._call(
                    "get", actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
                )
                if parent["sha256"] != ref["sha256"]:
                    raise error()
                stack.append(parent)
