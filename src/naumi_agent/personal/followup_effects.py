"""Human decisions and fenced, recoverable writes to original local domain owners."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta

from naumi_agent.personal.calendar_actions import action_digest
from naumi_agent.personal.connection_models import action_body, timestamp
from naumi_agent.personal.followup_models import (
    error,
    identity,
    normalize_reference,
    reference,
)
from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_models import metadata_identity, placement_identity

_APPLY = ContextVar("personal_followup_apply_proof", default=None)
_MUTABLE = frozenset(
    {
        "revision",
        "phase",
        "apply_lease",
        "artifact_ref",
        "target_result",
        "attempted",
        "receipt_missing",
        "last_error_code",
        "updated_at",
    }
)


class FollowupEffectsMixin:
    @staticmethod
    def _child_id(effect_id):
        return "followup-effect:" + effect_id

    def _target_payload(self, proposal, target, effect_id):
        if target["kind"] != proposal["effect_kind"]:
            raise error("invalid_input")
        child = self._child_id(effect_id)
        if target["kind"] == "today_place":
            if proposal["source"]["kind"] != "deadline":
                raise error("invalid_input")
            return {
                "request_id": child,
                "date": target["date"],
                "timezone": target["timezone"],
                "planner_id": proposal["actor_id"],
                "expected_day_revision": target["expected_day_revision"],
                "expected_metadata_revision": target["expected_metadata_revision"],
                "source_ref": copy.deepcopy(proposal["source"]["item_ref"]),
            }
        if proposal["source"]["kind"] != "calendar_change":
            raise error("invalid_input")
        return {
            "request_id": child,
            "connection_id": proposal["source"]["change_ref"]["connection_id"],
            "actor_id": proposal["actor_id"],
            "source_refs": [],
            "operation": "insert",
            "body": copy.deepcopy(target["body"]),
            "expires_at": target["expires_at"],
        }

    def _decision_tx(self, tx, proposal, expected, *, live=True):
        decision = tx.get("decision", self._virtual_decision(proposal)["id"], missing=True)
        decision = decision or self._virtual_decision(proposal)
        if (
            decision["workspace_key"] != self.workspace_key
            or decision["proposal_id"] != proposal["id"]
            or decision["actor_id"] != proposal["actor_id"]
        ):
            raise error("storage_error")
        if decision["revision"] != expected:
            raise error("version_conflict")
        if not live:
            return decision
        rule = tx.get("rule", proposal["rule_ref"]["id"])
        if (
            rule["workspace_key"] != self.workspace_key
            or rule["epoch"] != proposal["epoch"]
            or rule["status"] != "active"
            or rule["actor_digest"] != proposal["actor_digest"]
        ):
            raise error("source_stale")
        if digest(self._actor(proposal["actor_id"])) != proposal["actor_digest"]:
            raise error("source_stale")
        return decision

    async def _decision_source(self, payload, channel):
        self._manual(None, channel)
        proposal = await self._exact_proposal(payload["proposal_ref"])
        rule = await self._rule_for_proposal(proposal)
        await self._proposal_source(proposal, rule, current=True, fresh=True)
        if self.now() >= datetime.fromisoformat(proposal["expires_at"]):
            raise error("source_stale")
        return proposal

    async def _dismiss(self, payload, channel):
        self._manual(None, channel)
        proposal = await self._exact_proposal(payload["proposal_ref"])

        def save(tx):
            current = self._decision_tx(
                tx, proposal, payload["expected_decision_revision"], live=False
            )
            if current["status"] not in {"pending", "snoozed"}:
                raise error("version_conflict")
            saved = tx.put(
                "decision",
                {
                    **current,
                    "status": "dismissed",
                    "snooze_until": None,
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )
            return self._receipt(
                "dismiss",
                payload,
                {"proposal_id": proposal["id"], "decision_revision": saved["revision"]},
                proposal["actor_id"],
            )

        return await self._finish(await self._mutate("dismiss", payload, channel, save))

    async def _snooze(self, payload, channel):
        proposal = await self._decision_source(payload, channel)
        until = timestamp(payload["until"])
        if not self.now() + timedelta(minutes=1) <= until <= self.now() + timedelta(days=7):
            raise error("invalid_input")

        def save(tx):
            current = self._decision_tx(tx, proposal, payload["expected_decision_revision"])
            if current["status"] not in {"pending", "snoozed"}:
                raise error("version_conflict")
            saved = tx.put(
                "decision",
                {
                    **current,
                    "status": "snoozed",
                    "snooze_until": until.isoformat(timespec="microseconds"),
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )
            return self._receipt(
                "snooze",
                payload,
                {"proposal_id": proposal["id"], "decision_revision": saved["revision"]},
                proposal["actor_id"],
            )

        return await self._finish(await self._mutate("snooze", payload, channel, save))

    async def _accept(self, payload, channel):
        proposal = await self._decision_source(payload, channel)
        decision = await self._decision(proposal)
        replay = await self._replay("accept", payload, channel)
        if replay is not None:
            await self._apply_effect(replay["identity"]["effect_id"])
            return await self._finish(replay)
        retry_effect = None
        if decision["status"] == "accepted" and decision["effect_id"]:
            retry_effect, _, _ = await self._accepted_effect(decision["effect_id"])
            if retry_effect["phase"] != "blocked" or not retry_effect["receipt_missing"]:
                raise error("version_conflict")
            if await self._target_request(retry_effect) is not None:
                raise error("version_conflict")
        elif decision["status"] not in {"pending", "snoozed"}:
            raise error("version_conflict")
        target = copy.deepcopy(payload["target"])
        if target["kind"] == "calendar_draft":
            expires = timestamp(target["expires_at"])
            if not self.now() < expires <= self.now() + timedelta(hours=24):
                raise error("invalid_input")
            action_body(target["body"])
        effect_id = identity(
            "fe_", {"workspace_key": self.workspace_key, "request_id": payload["request_id"]}
        )
        frozen = self._target_payload(proposal, target, effect_id)

        def save(tx):
            current = self._decision_tx(tx, proposal, payload["expected_decision_revision"])
            if current["status"] == "accepted":
                if retry_effect is None or current["effect_id"] != retry_effect["id"]:
                    raise error("version_conflict")
                old = tx.get("effect", retry_effect["id"])
                if (
                    old["revision"] != retry_effect["revision"]
                    or old["phase"] != "blocked"
                    or not old["receipt_missing"]
                ):
                    raise error("version_conflict")
            elif current["status"] not in {"pending", "snoozed"}:
                raise error("version_conflict")
            effect = {
                "id": effect_id,
                "workspace_key": self.workspace_key,
                "actor_id": proposal["actor_id"],
                "actor_version": proposal["actor_version"],
                "actor_digest": proposal["actor_digest"],
                "proposal_ref": reference(proposal, "proposal"),
                "rule_ref": proposal["rule_ref"],
                "kind": proposal["effect_kind"],
                "target": target,
                "target_payload": frozen,
                "channel": channel,
                "accept_payload": copy.deepcopy(payload),
                "accept_request_id": payload["request_id"],
                "decision_revision": current["revision"] + 1,
                "phase": "pending",
                "apply_lease": None,
                "artifact_ref": None,
                "target_result": None,
                "attempted": False,
                "receipt_missing": False,
                "last_error_code": None,
                "created_at": self.stamp(),
                "updated_at": self.stamp(),
            }
            tx.put("effect", effect, expected_revision=0)
            saved = tx.put(
                "decision",
                {
                    **current,
                    "status": "accepted",
                    "effect_id": effect_id,
                    "snooze_until": None,
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )
            return self._receipt(
                "accept",
                payload,
                {
                    "proposal_id": proposal["id"],
                    "decision_revision": saved["revision"],
                    "effect_id": effect_id,
                },
                proposal["actor_id"],
            )

        receipt = await self._mutate("accept", payload, channel, save)
        await self._apply_effect(receipt["identity"]["effect_id"])
        return await self._finish(receipt)

    def _accepted_effect_tx(self, tx, effect_id):
        current = self._local(tx.get("effect", effect_id))
        initial = self._local(tx.get("effect", effect_id, revision=1))
        if (
            {k: v for k, v in current.items() if k not in _MUTABLE}
            != {k: v for k, v in initial.items() if k not in _MUTABLE}
            or initial["phase"] != "pending"
            or initial["apply_lease"] is not None
            or initial["artifact_ref"] is not None
            or initial["target_result"] is not None
            or initial["attempted"]
            or initial["receipt_missing"]
        ):
            raise error("storage_error")
        proposal = self._local(tx.get("proposal", initial["proposal_ref"]["proposal_id"]))
        if (
            reference(proposal, "proposal") != initial["proposal_ref"]
            or proposal["rule_ref"] != initial["rule_ref"]
            or proposal["actor_id"] != initial["actor_id"]
            or proposal["actor_version"] != initial["actor_version"]
            or proposal["actor_digest"] != initial["actor_digest"]
            or proposal["effect_kind"] != initial["kind"]
            or initial["target_payload"]
            != self._target_payload(proposal, initial["target"], effect_id)
        ):
            raise error("storage_error")
        request = tx.request(initial["accept_request_id"])
        payload = initial["accept_payload"]
        if (
            request is None
            or request["operation"] != "accept"
            or request["fingerprint"]
            != self._operation_fingerprint("accept", payload, initial["channel"])
            or payload["proposal_ref"] != initial["proposal_ref"]
            or payload["target"] != initial["target"]
            or payload["request_id"] != initial["accept_request_id"]
            or payload["expected_decision_revision"] + 1 != initial["decision_revision"]
            or request["result"]["workspace_key"] != self.workspace_key
            or request["result"]["actor_id"] != initial["actor_id"]
            or request["result"]["identity"]
            != {
                "proposal_id": proposal["id"],
                "decision_revision": initial["decision_revision"],
                "effect_id": effect_id,
            }
        ):
            raise error("storage_error")
        decision = tx.get(
            "decision",
            self._virtual_decision(proposal)["id"],
            revision=initial["decision_revision"],
        )
        if (
            decision["status"] != "accepted"
            or decision["effect_id"] != effect_id
            or decision["proposal_id"] != proposal["id"]
            or decision["workspace_key"] != self.workspace_key
            or decision["actor_id"] != initial["actor_id"]
            or initial["channel"] not in {"web", "cli", "tui"}
        ):
            raise error("storage_error")
        return current, initial, proposal

    async def _accepted_effect(self, effect_id):
        return await self._read(lambda tx: self._accepted_effect_tx(tx, effect_id))

    async def _authorize_effect_ref(self, raw, reader):
        ref = normalize_reference(raw, "effect")
        current, initial, proposal = await self._accepted_effect(ref["effect_id"])
        if reference(initial, "effect") != ref:
            raise error("source_stale")
        if reader is not None and reader != initial["actor_id"]:
            raise error("scope_denied")
        rule = await self._rule_for_proposal(proposal, active=False)
        await self._proposal_source(proposal, rule, current=False, fresh=False)
        await self._accepted_effect(initial["id"])
        return current

    async def _effect_artifact(self, effect):
        current, initial, proposal = await self._accepted_effect(effect["id"])
        ref = reference(initial, "effect")
        await self._authorize_effect_ref(ref, initial["actor_id"])
        content = canonical(
            {
                "effect_id": initial["id"],
                "proposal_ref": initial["proposal_ref"],
                "rule_ref": initial["rule_ref"],
                "source": proposal["source"],
                "target": initial["target"],
            }
        ).encode()
        if current["artifact_ref"] is None:
            snapshot = await self.artifacts.publish(
                actor_id=initial["actor_id"],
                request_id="followup-effect:" + initial["id"],
                title="已确认的跟进安排",
                kind="material",
                filename="followup-effect.txt",
                content=content,
                evidence={"source_kind": "followup_effect", "followup_effect_refs": [ref]},
            )
            artifact_ref = self.artifacts.reference(snapshot)

            def save(tx):
                latest, _, _ = self._accepted_effect_tx(tx, initial["id"])
                if latest["artifact_ref"] is not None and latest["artifact_ref"] != artifact_ref:
                    raise error("storage_error")
                if latest["artifact_ref"] is None:
                    tx.put(
                        "effect",
                        {**latest, "artifact_ref": artifact_ref, "updated_at": self.stamp()},
                        expected_revision=latest["revision"],
                    )

            await self._write(save)
        else:
            artifact_ref = current["artifact_ref"]
        snapshot, actual = await self.artifacts.read(
            actor_id=initial["actor_id"],
            artifact_id=artifact_ref["artifact_id"],
            version=artifact_ref["version"],
        )
        if (
            actual != content
            or snapshot["sha256"] != hashlib.sha256(content).hexdigest()
            or snapshot["evidence"].get("followup_effect_refs") != [ref]
            or self.artifacts.reference(snapshot) != artifact_ref
        ):
            raise error("storage_error")
        return artifact_ref

    def check_effect_commit(self, effect_id, proof, payload):
        if type(proof) is not dict or set(proof) != {"holder", "fence"}:
            raise error("scope_denied")
        self._guard()

        def read(tx):
            current, initial, _ = self._accepted_effect_tx(tx, effect_id)
            if (
                current["phase"] not in {"pending", "unknown"}
                or current["apply_lease"] != proof
                or canonical(payload) != canonical(initial["target_payload"])
            ):
                raise error("scope_denied")
            tx.require_lease("effect", effect_id, proof["holder"], proof["fence"], now=time.time())
            if digest(self._actor(initial["actor_id"])) != initial["actor_digest"]:
                raise error("source_stale")
            return True

        result = self.store.read(read)
        self._guard()
        return result

    async def resolve_effect(self, effect_id, expected_kind):
        binding = _APPLY.get()
        if binding is None or binding[:2] != (id(self), effect_id):
            raise error("scope_denied")
        proof = copy.deepcopy(binding[2])
        current, initial, proposal = await self._accepted_effect(effect_id)
        if initial["kind"] != expected_kind:
            raise error("scope_denied")
        rule = await self._rule_for_proposal(proposal, active=False)
        await self._proposal_source(proposal, rule, current=True, fresh=True)
        artifact = await self._effect_artifact(current)
        await asyncio.to_thread(
            self.check_effect_commit, effect_id, proof, initial["target_payload"]
        )
        return {
            "effect_id": effect_id,
            "payload": copy.deepcopy(initial["target_payload"]),
            "channel": initial["channel"],
            "actor_id": initial["actor_id"],
            "artifact_ref": artifact,
            "proof": proof,
        }

    async def _target_request(self, effect):
        payload = copy.deepcopy(effect["target_payload"])
        semantic = {**payload, "_followup_effect_id": effect["id"]}
        if effect["kind"] == "today_place":
            operation = "place"
            fingerprint = digest(
                {
                    "operation": operation,
                    "payload": self.today._envelope(semantic, None, effect["channel"]),
                }
            )
            request = await self.today._read(
                lambda tx: None if tx is None else tx.request(payload["request_id"])
            )
        else:
            operation = "prepare_action"
            fingerprint = digest(
                {
                    "operation": operation,
                    "payload": {
                        "workspace_key": self.workspace_key,
                        "caller_actor": None,
                        **semantic,
                    },
                }
            )
            request = await self.connections._read(
                lambda tx: None if tx is None else tx.request(payload["request_id"])
            )
        if request is not None and (
            request["operation"] != operation or request["fingerprint"] != fingerprint
        ):
            raise error("idempotency_conflict")
        return request

    async def _read_target(self, effect):
        current, initial, proposal = await self._accepted_effect(effect["id"])
        await self._authorize_effect_ref(reference(initial, "effect"), initial["actor_id"])
        request = await self._target_request(initial)
        if request is None:
            raise error("source_stale")
        artifact = await self._effect_artifact(current)
        payload, actor = initial["target_payload"], initial["actor_id"]
        if initial["kind"] == "today_place":
            identity_value = request["result"]["identity"]
            day = await self.today._record("day", identity_value["day_id"])
            historical = await self.today._record(
                "day", day["id"], revision=identity_value["day_revision"]
            )
            self.today._day_scope(historical, actor)
            self.today._day_scope(day, actor)
            if self.today._day_reference(historical) != identity_value["day_ref"]:
                raise error("storage_error")
            metadata_id = metadata_identity(self.workspace_key, payload["source_ref"]["item_id"])
            metadata_refs = identity_value["metadata_refs"]
            if len(metadata_refs) != 1 or metadata_refs[0]["metadata_id"] != metadata_id:
                raise error("storage_error")
            await self.today._authorized_metadata_ref(metadata_refs[0], actor)
            placement_id = placement_identity(day["id"], metadata_id)
            if (
                day["workspace_key"] != self.workspace_key
                or day["planner_id"] != actor
                or day["date"] != payload["date"]
                or day["timezone"] != payload["timezone"]
            ):
                raise error("storage_error")
            for value in (historical, day):
                placement = next((v for v in value["placements"] if v["id"] == placement_id), None)
                if (
                    placement is None
                    or placement["source_ref"] != payload["source_ref"]
                    or placement["metadata_id"] != metadata_id
                    or placement["disposition"] != "include"
                ):
                    raise error("source_stale")
                refs = [
                    ref
                    for context in value.get("provenance", [])
                    for ref in context["context"]["artifact_refs"]
                ]
                if artifact not in refs:
                    raise error("storage_error")
                await self.today._authorize_metadata_sources(value, actor)
            await self.today._authorize_item(payload["source_ref"], actor)
            result = {
                "kind": "today_place",
                "day_id": day["id"],
                "day_revision": day["revision"],
                "metadata_id": metadata_id,
                "placement_id": placement_id,
                "item_ref": payload["source_ref"],
            }
        else:
            action = await self.connections._action_record(request["result"]["id"], actor)
            expected = action_body(payload["body"])
            if (
                action["operation"] != "insert"
                or action["connection_id"] != payload["connection_id"]
                or action["actor_id"] != actor
                or action["actor_version"] != initial["actor_version"]
                or action["auth_epoch"] != proposal["source"]["change_ref"]["auth_epoch"]
                or action["source_refs"] != payload["source_refs"]
                or action["artifact_refs"] != [artifact]
                or {k: action["body"][k] for k in expected} != expected
                or timestamp(action["expires_at"]) != timestamp(payload["expires_at"])
                or action["digest"] != action_digest(action)
            ):
                raise error("source_stale")
            result = {
                "kind": "calendar_draft",
                "action_id": action["id"],
                "action_revision": action["revision"],
                "digest": action["digest"],
                "status": action["status"],
            }
        await self._authorize_effect_ref(reference(initial, "effect"), actor)
        return result

    async def _effect_update(self, effect_id, proof, **changes):
        def save(tx):
            current, _, _ = self._accepted_effect_tx(tx, effect_id)
            if current["apply_lease"] != proof:
                raise error("followup_busy")
            tx.require_lease("effect", effect_id, proof["holder"], proof["fence"], now=time.time())
            return tx.put(
                "effect",
                {**current, **changes, "updated_at": self.stamp()},
                expected_revision=current["revision"],
            )

        return await self._write(save)

    async def _apply_effect(self, effect_id):
        try:
            effect, _, _ = await self._accepted_effect(effect_id)
        except Exception as exc:
            self._last_error = error(getattr(exc, "code", "followup_unknown")).code
            return
        if effect["phase"] in {"materialized", "blocked"}:
            return
        holder = self.holder + ":" + uuid.uuid4().hex

        def claim(tx):
            current, _, _ = self._accepted_effect_tx(tx, effect_id)
            if current["phase"] not in {"pending", "unknown"}:
                return None
            lease = tx.claim("effect", effect_id, holder, now=time.time(), ttl=120)
            proof = {"holder": holder, "fence": lease["fence"]}
            tx.put(
                "effect",
                {**current, "apply_lease": proof, "updated_at": self.stamp()},
                expected_revision=current["revision"],
            )
            return proof

        try:
            proof = await self._write(claim)
        except Exception as exc:
            self._last_error = error(getattr(exc, "code", "followup_unknown")).code
            return
        if proof is None:
            return
        token = _APPLY.set((id(self), effect_id, proof))
        try:
            effect, initial, proposal = await self._accepted_effect(effect_id)
            receipt = await self._target_request(initial)
            if receipt is None:
                await self._effect_update(effect_id, proof, receipt_missing=True)
                rule = await self._rule_for_proposal(proposal, active=False)
                await self._proposal_source(proposal, rule, current=True, fresh=True)
                await self._effect_update(effect_id, proof, attempted=True, receipt_missing=False)
                if initial["kind"] == "today_place":
                    await self.today.place_from_followup(effect_id)
                else:
                    await self.connections.prepare_action_from_followup(effect_id)
                if await self._target_request(initial) is None:
                    raise error("followup_unknown")
            target = await self._read_target(effect)
            await self._effect_update(
                effect_id,
                proof,
                phase="materialized",
                target_result=target,
                receipt_missing=False,
                last_error_code=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, PersonalAgentError) else "followup_unknown"
            phase, missing = "unknown", False
            try:
                effect, initial, _ = await self._accepted_effect(effect_id)
                receipt = await self._target_request(initial)
                if receipt is not None:
                    target = await self._read_target(effect)
                    await self._effect_update(
                        effect_id,
                        proof,
                        phase="materialized",
                        target_result=target,
                        receipt_missing=False,
                        last_error_code=None,
                    )
                    return
                missing = True
                if isinstance(exc, PersonalAgentError) and code not in {
                    "followup_unknown",
                    "followup_busy",
                    "storage_error",
                    "lease_lost",
                    "idempotency_conflict",
                }:
                    phase = "blocked"
            except Exception:
                pass
            try:
                await self._effect_update(
                    effect_id,
                    proof,
                    phase=phase,
                    receipt_missing=missing,
                    last_error_code=error(code).code,
                )
            except Exception:
                self._last_error = self._last_error or "followup_unknown"
        finally:
            _APPLY.reset(token)
            try:
                await self._write(
                    lambda tx: tx.release(
                        "effect", effect_id, holder, proof["fence"], now=time.time()
                    )
                )
            except Exception:
                self._last_error = self._last_error or "followup_unknown"

    async def _reconcile(self, payload, channel):
        self._manual(None, channel)
        effect, _, proposal = await self._accepted_effect(payload["effect_id"])

        def save(tx):
            self._accepted_effect_tx(tx, effect["id"])
            decision = tx.get("decision", self._virtual_decision(proposal)["id"])
            return self._receipt(
                "reconcile",
                payload,
                {
                    "proposal_id": proposal["id"],
                    "decision_revision": decision["revision"],
                    "effect_id": effect["id"],
                },
                proposal["actor_id"],
            )

        receipt = await self._mutate("reconcile", payload, channel, save)
        await self._apply_effect(effect["id"])
        return await self._finish(receipt)

    async def _wake_and_recover(self, worker_lease):
        scan_id = "recovery:" + self.workspace_key
        scan = await self._record("scan", scan_id, missing=True) or {}
        decisions = await self._page(
            "decision", scan.get("decision_after"), limit=50, statuses={"snoozed"}
        )
        for decision in decisions["records"]:
            if self.now() < timestamp(decision["snooze_until"]):
                continue
            proposal = await self._proposal(decision["proposal_id"])
            try:
                rule = await self._rule_for_proposal(proposal)
                await self._proposal_source(proposal, rule, current=True, fresh=True)
                if self.now() >= timestamp(proposal["expires_at"]):
                    continue
            except PersonalAgentError as exc:
                self._last_error = error(exc.code).code
                continue

            def wake(tx):
                tx.require_lease(
                    "worker",
                    self.workspace_key,
                    self.holder,
                    worker_lease["fence"],
                    now=time.time(),
                )
                current = tx.get("decision", decision["id"])
                if current["revision"] != decision["revision"] or current["status"] != "snoozed":
                    return
                self._decision_tx(tx, proposal, current["revision"])
                tx.put(
                    "decision",
                    {
                        **current,
                        "status": "pending",
                        "snooze_until": None,
                        "presentation_epoch": current["presentation_epoch"] + 1,
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )

            await self._write(wake)
        effects = await self._page("effect", scan.get("effect_after"), limit=50)
        for effect in effects["records"]:
            if effect["phase"] in {"pending", "unknown"}:
                await self._read(
                    lambda tx: tx.require_lease(
                        "worker",
                        self.workspace_key,
                        self.holder,
                        worker_lease["fence"],
                        now=time.time(),
                    )
                )
                await self._apply_effect(effect["id"])

        def progress(tx):
            tx.require_lease(
                "worker", self.workspace_key, self.holder, worker_lease["fence"], now=time.time()
            )
            old = tx.get("scan", scan_id, missing=True)
            tx.put(
                "scan",
                {
                    "id": scan_id,
                    "workspace_key": self.workspace_key,
                    "decision_after": decisions["next_after"],
                    "effect_after": effects["next_after"],
                    "last_tick_at": self.stamp(),
                },
                expected_revision=old["revision"] if old else 0,
            )

        await self._write(progress)
