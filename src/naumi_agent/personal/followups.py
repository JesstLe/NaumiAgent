"""Durable source-driven followups; original planning, approval and execution stay owners."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from datetime import UTC, datetime

from naumi_agent.personal.followup_authority import FollowupAuthorityMixin
from naumi_agent.personal.followup_effects import FollowupEffectsMixin
from naumi_agent.personal.followup_models import (
    ACTOR,
    decision_view,
    error,
    identity,
    normalize_reference,
    record_digest,
    reference,
    rule_reference,
    rule_view,
)
from naumi_agent.personal.followup_persistence import FollowupPersistence
from naumi_agent.personal.followup_sources import FollowupSourcesMixin
from naumi_agent.personal.item_models import digest
from naumi_agent.personal.routine_schedule import _zone

logger = logging.getLogger(__name__)


class PersonalFollowupService(
    FollowupEffectsMixin, FollowupAuthorityMixin, FollowupSourcesMixin, FollowupPersistence
):
    def __init__(self, engine, *, store=None, clock=None):
        super().__init__(engine, store=store)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.holder = "followups-" + uuid.uuid4().hex
        self._worker = None
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self._last_error = None

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise error("storage_error")
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    @staticmethod
    def _manual(caller, channel):
        if caller is not None or channel not in {"web", "cli", "tui"}:
            raise error("scope_denied")

    def _actor(self, actor):
        if type(actor) is not str or not ACTOR.fullmatch(actor):
            raise error("scope_denied")
        if actor == "main":
            return {"id": "main", "name": "主 Agent", "version": 0}
        return self.personal.require_active_profile(actor).to_dict()

    @property
    def today(self):
        result = getattr(self.engine, "personal_today", None)
        if result is None:
            raise error()
        return result

    @property
    def items(self):
        return self.engine.personal_items

    @property
    def connections(self):
        result = getattr(self.engine, "personal_connections", None)
        if result is None:
            raise error()
        return result

    @property
    def artifacts(self):
        return self.today._artifacts()

    def _local(self, value, caller=None, *, actor_field="actor_id"):
        if value["workspace_key"] != self.workspace_key:
            raise error("not_found")
        actor = value[actor_field] if actor_field in value else value["body"]["actor_id"]
        if caller is not None and actor != caller:
            raise error("not_found")
        return value

    async def _page(self, kind, after=None, *, limit=50, statuses=None):
        return await self._read(
            lambda tx: (
                {"records": [], "next_after": None}
                if tx is None
                else tx.page(
                    kind, parent=self.workspace_key, after=after, limit=limit, statuses=statuses
                )
            )
        )

    async def _directory_id(self):
        return await self._read(
            lambda tx: (
                None
                if tx is None
                else tx.db.execute("SELECT instance_id FROM followup_meta").fetchone()[0]
            )
        )

    def _operation_fingerprint(self, operation, payload, channel):
        return digest(
            {
                "workspace_key": self.workspace_key,
                "operation": operation,
                "payload": payload,
                "channel": channel,
            }
        )

    async def _replay(self, operation, payload, channel):
        def read(tx):
            old = None if tx is None else tx.request(payload["request_id"])
            if old is None:
                return None
            if old["operation"] != operation or old["fingerprint"] != self._operation_fingerprint(
                operation, payload, channel
            ):
                raise error("idempotency_conflict")
            if old["result"]["workspace_key"] != self.workspace_key:
                raise error("not_found")
            return old["result"]

        return await self._read(read)

    async def _mutate(self, operation, payload, channel, callback):
        def save(tx):
            old = tx.request(payload["request_id"])
            fingerprint = self._operation_fingerprint(operation, payload, channel)
            if old:
                if old["operation"] != operation or old["fingerprint"] != fingerprint:
                    raise error("idempotency_conflict")
                return old["result"]
            result = callback(tx)
            tx.save_request(payload["request_id"], operation, fingerprint, result)
            return result

        return await self._write(save)

    def _receipt(self, operation, payload, identity_value, actor):
        return {
            "accepted": True,
            "operation": operation,
            "request_id": payload["request_id"],
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "identity": identity_value,
        }

    async def _rule(self, rule_id, caller=None, *, revision=None):
        return self._local(await self._record("rule", rule_id, revision=revision), caller)

    async def _rule_for_proposal(self, proposal, *, active=True):
        ref = proposal["rule_ref"]
        original = await self._rule(ref["id"], revision=ref["revision"])
        if record_digest(original) != ref["digest"] or original["epoch"] != proposal["epoch"]:
            raise error("storage_error")
        if active:
            current = await self._rule(ref["id"])
            if current["epoch"] != proposal["epoch"] or current["status"] != "active":
                raise error("source_stale")
        return original

    async def context(self, caller_actor=None):
        if caller_actor is not None:
            self._actor(caller_actor)
        page = await self._page("rule", limit=100)
        actors = (
            [{"id": "main", "name": "主 Agent", "version": 0}]
            + [
                {"id": p.id, "name": p.name, "version": p.version}
                for p in self.personal.list()
                if p.status == "active"
            ]
            if caller_actor is None
            else []
        )
        connections = []
        if caller_actor is None:
            try:
                values = await self.connections.list_connections("main")
                for value in values["connections"]:
                    connections.append(
                        {
                            k: value.get(k)
                            for k in ("id", "email", "can_write", "last_sync_at", "status")
                        }
                    )
            except Exception:
                connections = []
        scan = await self._record("scan", "workspace:" + self.workspace_key, missing=True)
        return {
            "workspace_key": self.workspace_key,
            "owner_key": self.owner_key,
            "configured": self.enrolled(),
            "actors": actors,
            "connections": connections,
            "worker": {
                "last_tick_at": scan.get("last_tick_at") if scan else None,
                "last_error_code": self._last_error
                or (scan.get("last_error_code") if scan else None),
                "partial": bool(page["next_after"] or scan and scan.get("partial")),
            },
            "server_now": self.stamp(),
        }

    async def rules(self, caller_actor=None):
        page = await self._page("rule", limit=1000)
        return {
            "rules": [
                rule_view(v)
                for v in page["records"]
                if caller_actor is None or v["body"]["actor_id"] == caller_actor
            ]
        }

    async def _save_rule(self, operation, payload, channel):
        await self.ensure()
        replay = await self._replay(operation, payload, channel)
        if replay is not None:
            return await self._finish(replay)
        body = copy.deepcopy(payload["rule"])
        profile = self._actor(body["actor_id"])
        if body["actor_version"] != profile["version"]:
            raise error("version_conflict")
        old = await self._rule(payload["rule_id"]) if operation == "update_rule" else None
        if old is not None and (
            old["body"]["actor_id"] != body["actor_id"] or old["status"] == "archived"
        ):
            raise error("scope_denied")

        def semantic(body_value):
            return {k: v for k, v in body_value.items() if k not in {"name", "status"}}

        reset = (
            old is None
            or semantic(body) != semantic(old["body"])
            or digest(profile) != old["actor_digest"]
        )
        binding = await self._source_binding(body) if reset else old["source_binding"]
        if not reset:
            await self._binding_authority(old)
        rule_id = (
            old["id"]
            if old
            else identity(
                "fr_", {"workspace": self.workspace_key, "request": payload["request_id"]}
            )
        )
        stamp = self.stamp()
        value = {
            "id": rule_id,
            "workspace_key": self.workspace_key,
            "body": body,
            "status": body["status"],
            "actor_digest": digest(profile),
            "timezone_sha256": _zone(body["timezone"])[1],
            "source_binding": binding,
            "epoch": uuid.uuid4().hex if reset else old["epoch"],
            "cutover_at": stamp if reset else old["cutover_at"],
            "created_at": old["created_at"] if old else stamp,
            "updated_at": stamp,
        }

        def save(tx):
            if old is None and len(tx.list("rule", parent=self.workspace_key, limit=101)) >= 100:
                raise error("input_limit")
            saved = tx.put("rule", value, expected_revision=payload.get("expected_revision", 0))
            return self._receipt(
                operation,
                payload,
                {"rule_id": saved["id"], "rule_revision": saved["revision"]},
                body["actor_id"],
            )

        receipt = await self._mutate(operation, payload, channel, save)
        return await self._finish(receipt)

    async def _set_status(self, payload, channel):
        rule = await self._rule(payload["rule_id"])
        if rule["status"] == "archived":
            raise error("version_conflict")
        if payload["status"] == "active":
            await self._binding_authority(rule)

        def save(tx):
            current = self._local(tx.get("rule", rule["id"]))
            value = {
                **current,
                "status": payload["status"],
                "body": {**current["body"], "status": payload["status"]},
                "updated_at": self.stamp(),
            }
            saved = tx.put("rule", value, expected_revision=payload["expected_revision"])
            return self._receipt(
                "set_status",
                payload,
                {"rule_id": saved["id"], "rule_revision": saved["revision"]},
                saved["body"]["actor_id"],
            )

        return await self._finish(await self._mutate("set_status", payload, channel, save))

    async def _proposal(self, proposal_id, caller=None):
        return self._local(await self._record("proposal", proposal_id), caller)

    async def _exact_proposal(self, raw, caller=None):
        ref = normalize_reference(raw, "proposal")
        value = await self._proposal(ref["proposal_id"], caller)
        if reference(value, "proposal") != ref:
            raise error("source_stale")
        return value

    def _virtual_decision(self, proposal):
        return {
            "id": identity("fd_", {"proposal": proposal["id"]}),
            "workspace_key": self.workspace_key,
            "proposal_id": proposal["id"],
            "actor_id": proposal["actor_id"],
            "revision": 0,
            "status": "pending",
            "presentation_epoch": 0,
            "snooze_until": None,
            "effect_id": None,
            "updated_at": proposal["created_at"],
        }

    async def _decision(self, proposal):
        empty = self._virtual_decision(proposal)
        return await self._record("decision", empty["id"], missing=True) or empty

    async def _view(self, proposal, caller=None):
        self._local(proposal, caller)
        decision = await self._decision(proposal)
        effect = (
            await self._record("effect", decision["effect_id"], missing=True)
            if decision["effect_id"]
            else None
        )
        result = {
            "id": proposal["id"],
            "workspace_key": self.workspace_key,
            "actor_id": proposal["actor_id"],
            "actor_version": proposal["actor_version"],
            "proposal_ref": reference(proposal, "proposal"),
            "decision": decision_view(decision),
            "availability": "source_unavailable",
            "title": None,
            "reason": None,
            "rule": None,
            "source": None,
            "effect_kind": proposal["effect_kind"],
            "effect_status": effect["phase"] if effect else "none",
            "target": None,
            "expires_at": proposal["expires_at"],
            "created_at": proposal["created_at"],
        }
        try:
            pending = decision["status"] in {"pending", "snoozed"}
            rule = await self._rule_for_proposal(proposal, active=pending)
            title, source = await self._proposal_source(
                proposal, rule, current=pending, fresh=pending
            )
            if pending and self.now() >= datetime.fromisoformat(proposal["expires_at"]):
                result["availability"] = "resolved"
                return result
            if effect and effect["phase"] == "materialized":
                result["target"] = await self._read_target(effect)
            reason = {
                "code": proposal["reason_code"],
                "nominal_at": proposal["nominal_at"],
                "observed_at": proposal["created_at"],
            }
            if "offset_minutes" in proposal:
                reason["offset_minutes"] = proposal["offset_minutes"]
            result.update(
                availability="available" if pending else "resolved",
                title=title,
                source=source,
                reason=reason,
                rule={**proposal["rule_ref"], "name": rule["body"]["name"]},
            )
            # Revalidate after all target/source awaits before returning a body.
            await self._proposal_source(proposal, rule, current=pending, fresh=pending)
        except Exception as exc:
            from naumi_agent.personal.models import PersonalAgentError

            if not isinstance(exc, PersonalAgentError):
                raise
            result["availability"] = (
                "source_stale"
                if exc.code in {"source_stale", "version_conflict", "timezone_data_changed"}
                else "source_unavailable"
            )
            result.update(title=None, reason=None, rule=None, source=None, target=None)
        return result

    async def get(self, proposal_id, caller_actor=None):
        return await self._view(await self._proposal(proposal_id, caller_actor), caller_actor)

    async def list(self, status="pending", actor_id=None, limit=50, after=None, caller_actor=None):
        actor = caller_actor if caller_actor is not None else actor_id
        if caller_actor is not None and actor_id is not None:
            raise error("scope_denied")
        if actor is not None:
            self._actor(actor)
        key = {
            "workspace_key": self.workspace_key,
            "actor": actor,
            "status": status,
            "limit": limit,
        }
        last = None
        if after:
            cursor = await self._record("cursor", after)
            if cursor["query"] != key:
                raise error("source_stale")
            last = cursor["after"]
        page = await self._page("proposal", last, limit=limit)
        values = []
        for proposal in page["records"]:
            if actor is not None and proposal["actor_id"] != actor:
                continue
            decision = await self._decision(proposal)
            if status != "all" and decision["status"] != status:
                continue
            values.append(await self._view(proposal, actor))
        cursor_id = None
        if page["next_after"] is not None:
            document = {
                "workspace_key": self.workspace_key,
                "query": key,
                "after": page["next_after"],
            }
            cursor_id = identity("fc_", document)
            await self._write(
                lambda tx: (
                    tx.get("cursor", cursor_id, missing=True)
                    or tx.put("cursor", {"id": cursor_id, **document}, expected_revision=0)
                )
            )
        return {
            "proposals": values,
            "next_after": cursor_id,
            "partial": cursor_id is not None
            or any(v["availability"] in {"source_stale", "source_unavailable"} for v in values),
            "server_now": self.stamp(),
        }

    async def _finish(self, receipt):
        result = {k: v for k, v in receipt.items() if k not in {"workspace_key", "actor_id"}}
        result["projection_available"] = False
        try:
            if receipt["identity"].get("rule_id"):
                rule = await self._rule(receipt["identity"]["rule_id"])
                if rule["revision"] != receipt["identity"]["rule_revision"]:
                    return result
                result["view"] = rule_view(rule)
            elif receipt["identity"].get("proposal_id"):
                view = await self.get(receipt["identity"]["proposal_id"])
                if (
                    view["availability"] in {"source_unavailable", "source_stale"}
                    or view["decision"]["revision"] != receipt["identity"].get("decision_revision")
                    or receipt["identity"].get("effect_id")
                    and view["decision"]["effect_id"] != receipt["identity"]["effect_id"]
                ):
                    return result
                result["view"] = view
            result["projection_available"] = True
        except Exception:
            result.pop("view", None)
        return result

    async def request_status(self, request_id, caller=None):
        receipt = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if (
            receipt is None
            or receipt["result"]["workspace_key"] != self.workspace_key
            or caller is not None
            and receipt["result"]["actor_id"] != caller
        ):
            return {"status": "missing", "request_id": request_id}
        return {"status": "accepted", **await self._finish(receipt["result"])}

    async def execute_operation(self, arguments, *, caller_actor=None, trusted_channel=None):
        from naumi_agent.personal.followup_tools import validate_followup_arguments

        fields = validate_followup_arguments(arguments, human=caller_actor is None)
        operation = fields.pop("operation")
        if caller_actor is not None:
            self._actor(caller_actor)
        if operation in {"context", "rules", "list", "get"}:
            return await getattr(self, operation)(**fields, caller_actor=caller_actor)
        self._manual(caller_actor, trusted_channel)
        if operation == "requests":
            return await self.request_status(fields["request_id"])
        payload = fields["payload"]
        replay = await self._replay(operation, payload, trusted_channel)
        if replay is not None:
            if replay["identity"].get("effect_id"):
                await self._apply_effect(replay["identity"]["effect_id"])
            return await self._finish(replay)
        try:
            if operation in {"create_rule", "update_rule"}:
                return await self._save_rule(operation, payload, trusted_channel)
            if operation == "set_status":
                return await self._set_status(payload, trusted_channel)
            return await getattr(self, "_" + operation)(payload, trusted_channel)
        except Exception:
            # Another exact request may commit while source validation awaits.
            # A stale projection is not proof that human acceptance was absent.
            committed = await self._replay(operation, payload, trusted_channel)
            if committed is None:
                raise
            return await self._finish(committed)

    async def tick(self):
        async with self._tick_lock:
            if not self.enrolled():
                return
            scan_id = "workspace:" + self.workspace_key
            scan = await self._record("scan", scan_id, missing=True)
            now = self.stamp()
            if scan and scan.get("max_wall") and now < scan["max_wall"]:
                self._last_error = "clock_rollback"
                return
            lease = await self._write(
                lambda tx: tx.claim(
                    "worker", self.workspace_key, self.holder, now=time.time(), ttl=120
                )
            )
            try:
                # The durable fence, not the per-object asyncio lock, owns this scan.
                scan = await self._record("scan", scan_id, missing=True)
                now = self.stamp()
                if scan and scan.get("max_wall") and now < scan["max_wall"]:
                    self._last_error = "clock_rollback"
                    return
                self._last_error = None
                page = await self._page("rule", scan.get("rule_after") if scan else None, limit=5)
                partial = False
                for rule in page["records"]:
                    if rule["status"] != "active":
                        continue
                    state_id = "rule:" + rule["id"] + ":" + rule["epoch"]
                    state = await self._record("scan", state_id, missing=True) or {"revision": 0}
                    try:
                        candidates, progress = await self._source_page(rule, state)
                        for candidate in candidates:
                            await self._save_candidate(rule, candidate, lease)

                        def save_progress(tx):
                            tx.require_lease(
                                "worker",
                                self.workspace_key,
                                self.holder,
                                lease["fence"],
                                now=time.time(),
                            )
                            current = tx.get("rule", rule["id"])
                            if record_digest(current) != record_digest(rule):
                                raise error("version_conflict")
                            tx.put(
                                "scan",
                                {
                                    **state,
                                    "id": state_id,
                                    "workspace_key": self.workspace_key,
                                    **progress,
                                    "last_tick_at": now,
                                },
                                expected_revision=state["revision"],
                            )

                        await self._write(save_progress)
                        partial = partial or progress["partial"]
                    except Exception as exc:
                        from naumi_agent.personal.models import PersonalAgentError

                        if not isinstance(exc, PersonalAgentError) or exc.code == "storage_error":
                            raise
                        partial = True
                        self._last_error = error(exc.code).code
                await self._wake_and_recover(lease)

                def save_scan(tx):
                    tx.require_lease(
                        "worker", self.workspace_key, self.holder, lease["fence"], now=time.time()
                    )
                    old = tx.get("scan", scan_id, missing=True)
                    if old and old.get("max_wall") and now < old["max_wall"]:
                        self._last_error = "clock_rollback"
                        raise error("clock_rollback")
                    tx.put(
                        "scan",
                        {
                            "id": scan_id,
                            "workspace_key": self.workspace_key,
                            "rule_after": page["next_after"],
                            "max_wall": now,
                            "last_tick_at": now,
                            "last_error_code": self._last_error,
                            "partial": partial or page["next_after"] is not None,
                        },
                        expected_revision=old["revision"] if old else 0,
                    )

                await self._write(save_scan)
            finally:
                try:
                    await self._write(
                        lambda tx: tx.release(
                            "worker",
                            self.workspace_key,
                            self.holder,
                            lease["fence"],
                            now=time.time(),
                        )
                    )
                except Exception:
                    self._last_error = self._last_error or "followup_unknown"

    async def _save_candidate(self, rule, candidate, lease):
        proposal_id = identity("fp_", candidate["key"])
        proposal = {
            "id": proposal_id,
            "workspace_key": self.workspace_key,
            "rule_ref": rule_reference(rule),
            "epoch": rule["epoch"],
            "actor_id": rule["body"]["actor_id"],
            "actor_version": rule["body"]["actor_version"],
            "actor_digest": rule["actor_digest"],
            "key_sha256": digest(candidate["key"]),
            "created_at": self.stamp(),
            **{k: v for k, v in candidate.items() if k != "key"},
        }
        await self._proposal_source(proposal, rule)

        def save(tx):
            tx.require_lease(
                "worker", self.workspace_key, self.holder, lease["fence"], now=time.time()
            )
            current = tx.get("rule", rule["id"])
            if record_digest(current) != record_digest(rule):
                raise error("version_conflict")
            old = tx.get("proposal", proposal_id, missing=True)
            if old is not None:
                if old["key_sha256"] != proposal["key_sha256"]:
                    raise error("storage_error")
                return old
            saved = tx.put("proposal", proposal, expected_revision=0)
            tx.put("decision", self._virtual_decision(saved), expected_revision=0)
            return saved

        await self._write(save)

    def start(self):
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(self._loop(), name="personal-followups")

    async def stop(self):
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _loop(self):
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from naumi_agent.personal.models import PersonalAgentError

                self._last_error = error(exc.code).code if isinstance(
                    exc, PersonalAgentError
                ) else "source_unavailable"
                logger.info("跟进扫描暂未完成，下一轮沿原进度核对")
            await asyncio.sleep(15)
