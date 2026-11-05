"""Exact followup model bytes and independent notification presentation references."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime

from naumi_agent.personal.followup_models import (
    error,
    identity,
    normalize_reference,
    record_digest,
    reference,
    rule_reference,
    rule_view,
)
from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_authority import _walking


class FollowupAuthorityMixin:
    async def notification_page(self, after=None, limit=50):
        page = await self._page("proposal", after, limit=limit)
        events, partial = [], page["next_after"] is not None
        instance = await self._directory_id()
        for proposal in page["records"]:
            decision = await self._decision(proposal)
            if decision["status"] != "pending":
                continue
            view = await self._view(proposal)
            if view["availability"] != "available":
                partial = partial or view["availability"] != "resolved"
                continue
            events.append(
                {
                    "proposal_ref": reference(proposal, "proposal"),
                    "decision_revision": decision["revision"],
                    "decision_digest": record_digest(decision),
                    "presentation_epoch": decision["presentation_epoch"],
                    "actor_id": proposal["actor_id"],
                    "actor_version": proposal["actor_version"],
                    "occurred_at": decision["updated_at"],
                    "store_instance": instance,
                }
            )
        return {"events": events, "next_after": page["next_after"], "partial": partial}

    def _notice_ref(self, raw):
        from naumi_agent.personal.followup_tools import FollowupNotificationSource

        try:
            if type(raw) is not dict:
                raise ValueError()
            normalize_reference(raw["proposal_ref"], "proposal")
            return FollowupNotificationSource.model_validate(raw).model_dump()
        except (ValueError, TypeError, KeyError):
            raise error() from None

    async def notification_projection(self, raw, reader):
        ref = self._notice_ref(raw)
        if reader is not None and reader != ref["actor_id"]:
            raise error()
        if ref["store_instance"] != await self._directory_id():
            raise error()
        proposal = await self._exact_proposal(ref["proposal_ref"], reader)
        if (
            proposal["actor_id"] != ref["actor_id"]
            or proposal["actor_version"] != ref["actor_version"]
        ):
            raise error()
        decision = await self._decision(proposal)
        resolved = {"title": None, "actor_name": None, "availability": "resolved", "jump": None}
        if (
            decision["status"] != "pending"
            or decision["revision"] != ref["decision_revision"]
            or record_digest(decision) != ref["decision_digest"]
            or decision["presentation_epoch"] != ref["presentation_epoch"]
            or decision["updated_at"] != ref["occurred_at"]
        ):
            return resolved
        view = await self._view(proposal, reader)
        if view["availability"] == "resolved":
            return resolved
        if view["availability"] != "available":
            raise error()
        return {
            "title": view["title"],
            "actor_name": self._actor(proposal["actor_id"])["name"],
            "availability": "available",
            "jump": {"kind": "followup", "id": proposal["id"]},
        }

    async def _read_authority(self, value, actor):
        if value["workspace_key"] != self.workspace_key or value["actor_id"] != actor:
            raise error()
        if value["actor_digest"] != digest(self._actor(actor)):
            raise error("source_stale")
        with _walking(self, ("followup-read", value["id"], record_digest(value), actor)) as cached:
            if cached:
                return
            for ref in value["rule_refs"]:
                rule = await self._rule(ref["id"], actor)
                if rule_reference(rule) != ref:
                    raise error("source_stale")
                await self._binding_authority(rule)
            for ref in value["proposal_refs"]:
                proposal = await self._exact_proposal(ref["proposal_ref"], actor)
                decision = await self._decision(proposal)
                if (
                    decision["revision"] != ref["decision_revision"]
                    or record_digest(decision) != ref["decision_digest"]
                ):
                    raise error("source_stale")
                pending = decision["status"] in {"pending", "snoozed"}
                rule = await self._rule_for_proposal(proposal, active=pending)
                await self._proposal_source(proposal, rule, current=pending, fresh=pending)
                if pending and self.now() >= datetime.fromisoformat(proposal["expires_at"]):
                    raise error("source_stale")
                if ref.get("target") is not None:
                    effect = await self._record("effect", decision["effect_id"])
                    if await self._read_target(effect) != ref["target"]:
                        raise error("source_stale")
            for ref in value["notification_refs"]:
                projection = await self.notification_projection(ref["source_ref"], actor)
                # Resolved records contain opaque facts, never an old source body.
                # Available title-bearing reads must not survive becoming resolved.
                if projection["availability"] != ref["availability"]:
                    raise error("source_stale")

    async def _read_record(
        self, body, actor, *, rule_refs=(), proposal_refs=(), notification_refs=()
    ):
        content = canonical(body).encode("utf-8")
        if len(content) > 1024 * 1024:
            raise error("input_limit")
        value = {
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "actor_digest": digest(self._actor(actor)),
            "body_sha256": hashlib.sha256(content).hexdigest(),
            "rule_refs": copy.deepcopy(list(rule_refs)),
            "proposal_refs": copy.deepcopy(list(proposal_refs)),
            "notification_refs": copy.deepcopy(list(notification_refs)),
        }
        value["id"] = identity("fl_", value)
        await self._read_authority(value, actor)

        def save(tx):
            old = tx.get("read", value["id"], missing=True)
            if old is not None:
                if {k: v for k, v in old.items() if k != "revision"} != value:
                    raise error("storage_error")
                return old
            return tx.put("read", value, expected_revision=0)

        return await self._write(save)

    async def notification_read_evidence(self, body, actor, refs):
        normalized = []
        for raw in refs:
            ref = self._notice_ref(raw)
            projection = await self.notification_projection(ref, actor)
            normalized.append({"source_ref": ref, "availability": projection["availability"]})
        if len(normalized) > 100:
            raise error("input_limit")
        await self.today._read_session(actor)
        record = await self._read_record(body, actor, notification_refs=normalized)
        await self._read_authority(record, actor)
        return reference(record, "read")

    async def model_result(self, operation, result, actor, arguments):
        fields = {k: v for k, v in arguments.items() if k != "operation"}
        body = await getattr(self, operation)(**fields, caller_actor=actor)
        rule_refs, proposal_refs = [], []
        if operation == "context":
            body = {k: body[k] for k in ("workspace_key", "configured", "worker", "server_now")}
        elif operation == "rules":
            filtered = []
            for view in body["rules"]:
                try:
                    original = await self._rule(view["id"], actor)
                    await self._binding_authority(original)
                except PersonalAgentError:
                    continue
                if rule_view(original) != view:
                    raise error("source_stale")
                filtered.append(view)
                rule_refs.append(rule_reference(original))
            body = {"rules": filtered}
        else:
            entries = body["proposals"] if operation == "list" else [body]
            for entry in entries:
                if entry["title"] is None:
                    continue
                proposal = await self._exact_proposal(entry["proposal_ref"], actor)
                decision = await self._decision(proposal)
                if decision_view := entry.get("decision"):
                    if decision_view["revision"] != decision["revision"]:
                        raise error("source_stale")
                proposal_refs.append(
                    {
                        "proposal_ref": entry["proposal_ref"],
                        "decision_revision": decision["revision"],
                        "decision_digest": record_digest(decision),
                        "target": entry["target"],
                    }
                )
        if not self.enrolled():
            return body
        body = copy.deepcopy(body)
        session, version = await self.today._read_session(actor)
        record = await self._read_record(
            body, actor, rule_refs=rule_refs, proposal_refs=proposal_refs
        )
        content = canonical(body).encode("utf-8")
        snapshot = await self.artifacts.publish(
            actor_id=actor,
            request_id="followup-read:" + record["id"],
            title="跟进",
            kind="material",
            filename="followups.txt",
            content=content,
            source_refs=[],
            evidence={
                "source_kind": "followup_read",
                "followup_read_refs": [reference(record, "read")],
            },
        )
        if snapshot["sha256"] != record["body_sha256"]:
            raise error("storage_error")
        await self.artifacts.bind_refs(session.id, actor, [self.artifacts.reference(snapshot)])
        if (await self.today._read_session(actor, session))[1] != version:
            raise error("source_stale")
        await self._read_authority(record, actor)
        return body

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
            with _walking(self, ("followup-artifact", *key)) as cached:
                if cached:
                    continue
                for raw in value.get("evidence", {}).get("followup_read_refs", []):
                    ref = normalize_reference(raw, "read")
                    if ref["workspace_key"] != self.workspace_key:
                        raise error()
                    record = await self._record("read", ref["read_id"], revision=1)
                    if reference(record, "read") != ref or value["sha256"] != record["body_sha256"]:
                        raise error()
                    await self._read_authority(record, actor_id)
                for raw in value.get("evidence", {}).get("followup_effect_refs", []):
                    effect = await self._authorize_effect_ref(raw, actor_id)
                    proposal = await self._exact_proposal(effect["proposal_ref"], actor_id)
                    content = canonical({
                        "effect_id": effect["id"],
                        "proposal_ref": effect["proposal_ref"],
                        "rule_ref": effect["rule_ref"],
                        "source": proposal["source"],
                        "target": effect["target"],
                    }).encode("utf-8")
                    if value["sha256"] != hashlib.sha256(content).hexdigest():
                        raise error("source_stale")
            for ref in value.get("source_refs", []):
                original = await self.artifacts._call(
                    "get", actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
                )
                if original["sha256"] != ref["sha256"]:
                    raise error()
                stack.append(original)
