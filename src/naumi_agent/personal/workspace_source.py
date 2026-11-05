"""Source-runtime capability authority over original Items, Jobs and material grants."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from datetime import timedelta

from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_models import item_ref
from naumi_agent.personal.workspace_models import (
    MAX_BODY_BYTES,
    FederationRequest,
    Ticket,
    error,
    grant_view,
    identity,
    normalize,
    timestamp,
)
from naumi_agent.personal.workspace_transport import TRACE, request_signature, response_signature

FEDERATION_TIMEOUT_SECONDS = 15


class WorkspaceSourceMixin:
    async def _source_identity(self, *, enroll=False):
        if enroll:
            await self.ensure()
            item_instance = await self.items._call("ensure_identity")
        else:
            item_instance = await self.items._call("identity")
        directory_instance = await self._directory_id()
        if not directory_instance or not item_instance or not self.enrolled():
            raise error()
        return {
            "version": 1,
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "session_db_key": self.personal.session_db_key,
            "directory_instance_id": directory_instance,
            "item_instance_id": item_instance,
        }

    async def _source_grant(self, grant_id, *, token=None, recipient=None):
        value = await self._record("grant", grant_id)
        if value["status"] != "active":
            raise error("grant_revoked")
        if self.now() >= timestamp(value["expires_at"]):

            def expire(tx):
                current = self._local(tx.get("grant", grant_id))
                if current["status"] == "active":
                    tx.put(
                        "grant",
                        {
                            **current,
                            "status": "revoked",
                            "revoked_at": self.stamp(),
                            "revocation_reason": "expired",
                        },
                        expected_revision=current["revision"],
                    )

            await self._write(expire)
            raise error("grant_revoked")
        if self.now() < timestamp(value["created_at"]):
            raise error("source_stale")
        if token is not None and not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(), value["token_sha256"]
        ):
            raise error("grant_revoked")
        if recipient is not None and value["recipient"] != recipient:
            raise error("grant_revoked")
        if value["source"] != await self._source_identity():
            raise error("source_stale")
        if digest(self._actor_profile(value["source_actor_id"])) != value["actor_digest"]:
            raise error("source_stale")
        if value["file_bindings"] != self._file_bindings():
            raise error("source_stale")
        return value

    async def _ticket(self, grant):
        grant = await self._source_grant(grant["id"])
        value = await self._vault_open(grant["id"], grant["envelope"])
        ticket = {
            "version": 1,
            "grant_id": grant["id"],
            "grant_revision": 1,
            **{
                key: grant[key]
                for key in (
                    "source",
                    "recipient",
                    "source_actor_id",
                    "source_actor_version",
                    "scopes",
                    "expires_at",
                )
            },
            "secret": value["secret"],
        }
        return normalize(Ticket, ticket)

    async def issue(self, payload, *, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        await self.ensure()
        await self._prepare_route_files()
        prior = await self._replay("issue", payload)
        if prior is not None:
            return await self._issue_projection(prior)
        expires = timestamp(payload["expires_at"])
        if not self.now() < expires <= self.now() + timedelta(days=30):
            raise error("invalid_input")
        profile = self._actor_profile(payload["source_actor_id"])
        if profile["version"] != payload["source_actor_version"]:
            raise error("version_conflict")
        if payload["recipient"]["workspace_key"] == self.workspace_key:
            raise error("invalid_input")
        source = await self._source_identity(enroll=True)
        grant_id = identity(
            "wg_", {"workspace": self.workspace_key, "request": payload["request_id"]}
        )
        secret = secrets.token_hex(32)
        async with self._slot(grant_id) as lease:
            envelope = await self._vault_seal(grant_id, {"secret": secret})

            def save(tx):
                # The profile/identity checks are repeated outside the commit before export.
                existing = tx.page("grant", parent=self.workspace_key, limit=1000)
                if existing["next_after"] or len(existing["records"]) >= 1000:
                    raise error("input_limit")
                value = {
                    "id": grant_id,
                    "workspace_key": self.workspace_key,
                    "status": "active",
                    "source": source,
                    "recipient": payload["recipient"],
                    "source_actor_id": payload["source_actor_id"],
                    "source_actor_version": payload["source_actor_version"],
                    "actor_digest": digest(profile),
                    "file_bindings": self._file_bindings(),
                    "scopes": sorted(payload["scopes"]),
                    "expires_at": expires.isoformat(timespec="microseconds"),
                    "created_at": self.stamp(),
                    "token_sha256": hashlib.sha256(secret.encode()).hexdigest(),
                    "envelope": envelope,
                }
                value = tx.put("grant", value, expected_revision=0)
                return self._receipt(payload, "issue", value)

            receipt = await self._mutate("issue", payload, save, lease=lease)
        return await self._issue_projection(receipt)

    async def _issue_projection(self, receipt):
        try:
            grant = await self._record("grant", receipt["identity"]["id"])
            return {**receipt, "grant": grant_view(grant), "ticket": await self._ticket(grant)}
        except Exception:
            # Durable acceptance survives a late vault/source projection failure.
            return {**receipt, "projection_available": False, "content_transport": "requests"}

    async def revoke(self, payload, *, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)

        def save(tx):
            grant = self._local(tx.get("grant", payload["grant_id"]))
            if grant["status"] != "active":
                raise error("version_conflict")
            value = tx.put(
                "grant",
                {**grant, "status": "revoked", "revoked_at": self.stamp()},
                expected_revision=payload["expected_revision"],
            )
            return self._receipt(payload, "revoke", value)

        return await self._mutate("revoke", payload, save)

    async def _export_entry(self, item, actor):
        today = self._today()
        ref = item_ref(item)
        await today._authorize_item(ref, actor, require_current=True)
        view = await self.items._view(item, actor)
        occurrence = view["occurrence"]
        if occurrence and occurrence.get("source_available") is False:
            raise error()
        definition = item["definition"]
        entry = {
            "item_id": item["item_id"],
            "item_revision": item["revision"],
            "once_key": item["once_key"],
            "definition_sha256": item["definition_sha256"],
            "title": definition["title"],
            "state": item["state"],
            "assignee_id": definition["assignee"]["id"],
            "assignee_name": definition["assignee"]["name"],
            "schedule": {key: definition["schedule"][key] for key in ("due_at", "timezone")},
            "execution": (
                {
                    key: occurrence.get(key, "")
                    for key in ("status", "occurrence_id", "job_id", "run_id")
                }
                if occurrence
                else None
            ),
            "source_ref": ref,
        }
        await today._authorize_item(ref, actor, require_current=True)
        return entry

    async def _export_page(self, fields, grant):
        actor = grant["source_actor_id"]
        self._today()._artifacts()
        page = await self.items.source_page(
            limit=fields.get("limit", 50),
            after=fields.get("after") or "",
            caller_actor=actor,
        )
        entries, refs, withheld = [], [], False
        for item in page["items"]:
            try:
                entry = await self._export_entry(item, actor)
            except PersonalAgentError as exc:
                # Authorization failures hide this body but retain the raw keyset continuation.
                if exc.code in {"storage_error", "configuration_error"}:
                    raise
                withheld = True
                continue
            entries.append(entry)
            refs.append(entry["source_ref"])
        return {
            "entries": entries,
            "next_after": page["next_after"],
            "partial": withheld or page["next_after"] is not None,
        }, {"item_refs": refs, "results": []}

    @staticmethod
    def _result_identity(value):
        return {
            key: value.get(key)
            for key in (
                "occurrence_id",
                "status",
                "job_id",
                "run_id",
                "delivery_id",
                "delivery_sha256",
                "publication_id",
                "artifacts",
            )
        }

    async def _export_result(self, fields, grant):
        if "results" not in grant["scopes"]:
            raise error("scope_denied")
        actor = grant["source_actor_id"]
        item = await self.items._item(fields["item_id"], actor)
        entry = await self._export_entry(item, actor)
        intent = await self.items._current_intent(item)
        if intent is None:
            raise error("not_found")
        result = await self.items.result(intent["occurrence_id"], caller_actor=actor)
        if not result.get("source_available", True) or not result.get("output_available"):
            raise error()
        artifacts, count = [], 0
        for ref in result["artifacts"]:
            snapshot, content = await self._artifacts().read(
                actor_id=actor,
                artifact_id=ref["artifact_id"],
                version=ref["version"],
            )
            if snapshot["sha256"] != ref["sha256"]:
                raise error("source_stale")
            count += len(content)
            if len(content) > 256 * 1024 or count > 512 * 1024:
                raise error("input_limit")
            try:
                body = content.decode("utf-8")
            except UnicodeError:
                raise error("input_limit") from None
            artifacts.append(
                {key: snapshot[key] for key in ("artifact_id", "version", "sha256", "title")}
                | {"content_text": body}
            )
        history = await self.items._call("history", item_id=item["item_id"])
        original = next((v for v in history if v["revision"] == result["item_revision"]), None)
        if original is None:
            raise error()
        refs = list({canonical(v): v for v in (item_ref(item), item_ref(original))}.values())
        body = {
            "item": entry,
            "result": {
                **{
                    key: result.get(key)
                    for key in (
                        "occurrence_id",
                        "status",
                        "job_id",
                        "run_id",
                        "answer",
                        "delivery_id",
                        "delivery_sha256",
                    )
                },
                "artifacts": artifacts,
                "read_receipt": None,
            },
        }
        return body, {"item_refs": refs, "results": [self._result_identity(result)]}

    async def _source_read_authority(self, record, grant):
        if (
            record["grant_id"] != grant["id"]
            or record["grant_revision"] != grant["revision"]
            or record["source"] != grant["source"]
            or record["recipient"] != grant["recipient"]
            or record["source_actor_id"] != grant["source_actor_id"]
        ):
            raise error("source_stale")
        actor = grant["source_actor_id"]
        for ref in record["item_refs"]:
            await self._today()._authorize_item(ref, actor)
        for original in record["results"]:
            current = await self.items.result(original["occurrence_id"], caller_actor=actor)
            if (
                not current.get("source_available", True)
                or not current.get("output_available")
                or self._result_identity(current) != original
            ):
                raise error()
            await self._artifacts().authorize_refs(actor_id=actor, refs=original["artifacts"])

    async def _save_source_read(self, body, manifest, grant):
        content = canonical(body).encode()
        if len(content) > MAX_BODY_BYTES:
            raise error("input_limit")
        value = {
            "workspace_key": self.workspace_key,
            "grant_id": grant["id"],
            "grant_revision": grant["revision"],
            "source": grant["source"],
            "recipient": grant["recipient"],
            "source_actor_id": grant["source_actor_id"],
            "body_sha256": hashlib.sha256(content).hexdigest(),
            **manifest,
        }
        value["id"] = identity("ws_", value)
        await self._source_read_authority(value, grant)

        def save(tx):
            current = self._local(tx.get("grant", grant["id"]))
            if current != grant:
                raise error("grant_revoked")
            old = tx.get("source_read", value["id"], missing=True)
            return old or tx.put("source_read", value, expected_revision=0)

        saved = await self._write(save)
        return {
            "read_id": saved["id"],
            "revision": 1,
            "digest": digest(saved),
            "body_sha256": saved["body_sha256"],
        }

    async def federation(self, arguments, *, token, trace=()):
        try:
            async with asyncio.timeout(FEDERATION_TIMEOUT_SECONDS):
                return await self._federation(arguments, token=token, trace=trace)
        except TimeoutError:
            raise error() from None

    async def _federation(self, arguments, *, token, trace=()):
        fields = normalize(FederationRequest, arguments)
        proof = (
            re.fullmatch(r"NaumiWorkspace ([a-f0-9]{32}):([1-9][0-9]{0,15}):([a-f0-9]{64})", token)
            if type(token) is str
            else None
        )
        if (
            proof is None
            or not isinstance(trace, (tuple, list))
            or len(trace) >= 8
            or len(trace) != len(set(trace))
            or any(
                type(v) is not str or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
                for v in trace
            )
        ):
            raise error("grant_revoked")
        marker = self._trace_marker()
        if marker in trace:
            raise error()
        trace_token = TRACE.set((*trace, marker))
        try:
            async with self._source_semaphore:
                grant = await self._source_grant(fields["grant_id"], recipient=fields["recipient"])
                secret = (await self._vault_open(grant["id"], grant["envelope"]))["secret"]
                nonce, stamp, signature = proof.groups()
                if (
                    abs(time.time() - int(stamp)) > 60
                    or not hmac.compare_digest(
                        request_signature(secret, nonce, stamp, arguments, trace), signature
                    )
                    or not hmac.compare_digest(
                        hashlib.sha256(secret.encode()).hexdigest(), grant["token_sha256"]
                    )
                ):
                    raise error("grant_revoked")

                def admit(tx):
                    holder = secrets.token_hex(16)
                    key = grant["id"] + ":" + nonce
                    lease = tx.claim(
                        "federation_nonce", key, holder, now=time.time(), ttl=60, once=True
                    )
                    tx.require_lease(
                        "federation_nonce",
                        key,
                        holder,
                        lease["fence"],
                        now=time.time(),
                        phase="sent",
                    )

                await self._write(admit)
                operation = fields["operation"]
                read_ref = None
                if operation == "inspect":
                    body = {}
                elif operation == "validate":
                    ref = fields["read_ref"]
                    record = await self._record("source_read", ref["read_id"], revision=1)
                    if (
                        digest(record) != ref["digest"]
                        or record["body_sha256"] != ref["body_sha256"]
                    ):
                        raise error("source_stale")
                    await self._source_read_authority(record, grant)
                    body, read_ref = {"valid": True}, ref
                else:
                    body, manifest = await (
                        self._export_page(fields, grant)
                        if operation == "page"
                        else self._export_result(fields, grant)
                    )
                    read_ref = await self._save_source_read(body, manifest, grant)
                latest = await self._source_grant(grant["id"], recipient=fields["recipient"])
                if latest != grant:
                    raise error("grant_revoked")
                response = {
                    "source": grant["source"],
                    "grant": grant_view(grant),
                    "body": body,
                    "read_ref": read_ref,
                }
                return {
                    **response,
                    "proof": {
                        "nonce": nonce,
                        "signature": response_signature(secret, nonce, response),
                    },
                }
        finally:
            TRACE.reset(trace_token)
