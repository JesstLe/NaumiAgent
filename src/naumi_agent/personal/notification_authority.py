"""Notification references never become independent authority over original sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from naumi_agent.daemons.agent_jobs import AgentJobError, agent_job_id_for_request
from naumi_agent.personal.calendar_actions import action_digest
from naumi_agent.personal.item_execution import _request
from naumi_agent.personal.item_models import canonical, digest, keys
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import identity, record_digest
from naumi_agent.personal.today_authority import _walking
from naumi_agent.personal.today_models import item_ref


def unavailable():
    return PersonalAgentError("source_unavailable", "原通知来源已失效，请在原事项或行动中核对")


class NotificationAuthorityMixin:
    def _today(self):
        today = getattr(self.engine, "personal_today", None)
        if today is None:
            raise unavailable()
        return today

    async def _item_event(self, raw, reader=None):
        intent = self.items.executor._load(raw)
        if reader is not None and reader != intent["actor_id"]:
            raise unavailable()
        history = await self.items._call("history", item_id=intent["item_id"])
        original = next((v for v in history if v["revision"] == intent["item_revision"]), None)
        if original is None:
            raise self._broken()
        ref = item_ref(original)
        await self._today()._authorize_item(ref, reader)
        artifacts = self._today()._artifacts()
        for actor in {reader, intent["actor_id"]} - {None}:
            await artifacts.authorize_refs(actor_id=actor, refs=intent["input_refs"])
        request = _request(intent)
        jobs = self.items.executor.jobs
        try:
            job = await jobs.get(agent_job_id_for_request(request))
            if job is None:
                return None, False
            if job.request != request:
                raise self._broken()
            state = job.state.value
            base = {
                "workspace_key": self.workspace_key,
                "route": self.items._route(),
                "item_instance": await asyncio.to_thread(
                    self.items.store.identity, owner_key=self.owner_key
                ),
                "item_ref": ref,
                "occurrence_id": intent["occurrence_id"],
                "prepared_sha256": intent["prepared_sha256"],
                "execution_session_id": intent["execution_session_id"],
                "job_id": job.job_id,
                "request_sha256": job.request_sha256,
                "actor_id": intent["actor_id"],
                "actor_version": original["definition"]["assignee"]["version"],
            }
            if state in {"error", "timeout", "max_turns"}:
                # get() verifies the authenticated, chained lifecycle receipt, including
                # infrastructure failures without a personal-item result manifest.
                source = {
                    **base,
                    "terminal_receipt_id": job.latest_receipt.receipt_id,
                    "terminal_receipt_sha256": job.latest_receipt.receipt_sha256,
                    "state": state,
                    "result_sha256": job.latest_receipt.result_sha256,
                }
                kind, occurred = "item_failure", job.latest_receipt.occurred_at
            elif state == "completed":
                view = await self.items.executor.view(intent)
                if not view.get("source_available", True):
                    raise unavailable()
                if not view.get("delivery_id"):
                    return None, False
                source = {
                    **base,
                    **{
                        k: view[k]
                        for k in (
                            "run_id",
                            "publication_id",
                            "delivery_id",
                            "delivery_sha256",
                            "delivered_at",
                        )
                    },
                }
                kind, occurred = "item_result", view["delivered_at"]
            else:
                return None, state == "cancelled"
        except AgentJobError:
            raise self._broken() from None
        event = {
            "id": identity("ne_", {"kind": kind, "source_ref": source}),
            "workspace_key": self.workspace_key,
            "kind": kind,
            "actor_id": intent["actor_id"],
            "source_ref": source,
            "occurred_at": occurred,
        }
        return (event, True)

    async def _action_event(self, action):
        service = getattr(self.engine, "personal_connections", None)
        if service is None:
            raise unavailable()
        service._action_scope(action)
        # Expiry is an original civil instant, never a freshly extended deadline.
        from datetime import datetime

        if action["status"] != "draft" or service.now() >= datetime.fromisoformat(
            action["expires_at"]
        ):
            return None
        actor = action["actor_id"]
        if service.actor_version(actor) != action["actor_version"]:
            raise unavailable()
        connection = await service._connection(action["connection_id"], actor, write=True)
        await service._read(
            lambda tx: service._action_current(
                tx,
                action,
                write=True,
                expiry=False,
                approved=False,
            )
        )
        if connection["auth_epoch"] != action["auth_epoch"]:
            raise unavailable()
        await service.authorize_sources(action["source_refs"], actor)
        await service._authorize_action_dependencies(action)
        await asyncio.to_thread(service._guard)
        source = {
            "action_id": action["id"],
            "action_revision": action["revision"],
            "action_digest": action_digest(action),
            "draft_revision": action["draft_revision"],
            "workspace_key": self.workspace_key,
            "connection_id": action["connection_id"],
            "auth_epoch": action["auth_epoch"],
            "expires_at": action["expires_at"],
            "actor_id": actor,
            "actor_version": action["actor_version"],
            "connection_instance": service._required_identity,
        }
        return {
            "id": identity("ne_", {"kind": "calendar_approval", "source_ref": source}),
            "workspace_key": self.workspace_key,
            "kind": "calendar_approval",
            "actor_id": actor,
            "source_ref": source,
            "occurred_at": action["updated_at"],
        }

    async def _authorize_event(self, event, reader):
        self._actor(reader)
        if event["workspace_key"] != self.workspace_key:
            raise unavailable()
        ref, actor = event["source_ref"], event["actor_id"]
        if reader is not None and actor != reader:
            raise unavailable()
        if event["id"] != identity("ne_", {"kind": event["kind"], "source_ref": ref}):
            raise self._broken()
        if event["kind"] in {"item_result", "item_failure"}:
            intent = await self.items._call("get_intent", occurrence_id=ref["occurrence_id"])
            actual, _ = await self._item_event(intent, reader)
            if actual is None or actual != {k: v for k, v in event.items() if k != "revision"}:
                raise unavailable()
            original, _ = await self._today()._authorize_item(ref["item_ref"], reader)
            # The notification contains no answer, artifact text, error body or paths.
            return {
                "title": original["definition"]["title"],
                "actor_name": original["definition"]["assignee"]["name"],
                "availability": "available",
                "jump": {
                    "kind": "result" if event["kind"] == "item_result" else "item",
                    "id": ref["occurrence_id"]
                    if event["kind"] == "item_result"
                    else ref["item_ref"]["item_id"],
                },
            }
        if event["kind"] != "calendar_approval":
            raise self._broken()
        service = getattr(self.engine, "personal_connections", None)
        if service is None:
            raise unavailable()
        original = service._action_scope(
            await service._record("action", ref["action_id"], revision=ref["action_revision"]),
            reader,
        )
        current = await service._action_record(ref["action_id"], actor)
        connection = await service._connection(ref["connection_id"], actor, write=True)
        await service._read(
            lambda tx: service._action_current(
                tx,
                original,
                write=True,
                expiry=False,
                approved=False,
            )
        )
        await asyncio.to_thread(service._guard)
        if (
            service._required_identity != ref["connection_instance"]
            or action_digest(original) != ref["action_digest"]
            or original["draft_revision"] != ref["draft_revision"]
            or connection["auth_epoch"] != ref["auth_epoch"]
            or original["actor_id"] != actor
            or original["expires_at"] != ref["expires_at"]
        ):
            raise unavailable()
        await service.authorize_sources(original["source_refs"], actor)
        await service._authorize_action_dependencies(original)
        from datetime import datetime

        resolved = (
            current["status"] != "draft"
            or current["digest"] != ref["action_digest"]
            or service.now() >= datetime.fromisoformat(ref["expires_at"])
        )
        return {
            "title": "日历行动待审批" if not resolved else "日历行动已结束",
            "actor_name": "主 Agent" if actor == "main" else self.personal.get(actor).name,
            "availability": "resolved" if resolved else "available",
            "jump": {"kind": "calendar_action", "id": ref["action_id"]},
        }

    async def _scan_sources(self, scan, prefs):
        page = await self.items._call(
            "list_intent_reference_page",
            workspace_key=self.workspace_key,
            after_occurrence_id=scan["intent_after"] or "",
            limit=50,
        )
        watches_page = await self._page(
            "watch", scan["watch_after"], limit=50, statuses={"pending"}
        )
        intents = {v["occurrence_id"]: v for v in page["intents"]}
        for watch in watches_page["records"]:
            try:
                intent = await self.items._call("get_intent", occurrence_id=watch["occurrence_id"])
                intents[intent["occurrence_id"]] = intent
            except PersonalAgentError as exc:
                if exc.code == "storage_error":
                    raise
        events, watches = [], []
        source_errors = False
        for intent in intents.values():
            done = False
            try:
                event, done = await self._item_event(intent)
                if event:
                    # A second authorization before the short commit prevents a stale
                    # source projection from being treated as a fresh readable fact.
                    await self._authorize_event(event, None)
                    events.append(event)
            except PersonalAgentError as exc:
                if exc.code == "storage_error":
                    raise
                source_errors = True
            watches.append(
                {
                    "id": identity("nw_", {"occurrence_id": intent["occurrence_id"]}),
                    "workspace_key": self.workspace_key,
                    "occurrence_id": intent["occurrence_id"],
                    "status": "done" if done else "pending",
                }
            )
        connections = getattr(self.engine, "personal_connections", None)
        action_page = {"records": [], "next_after": None}
        if connections is not None:
            action_page = await connections._read(
                lambda tx: (
                    {"records": [], "next_after": None}
                    if tx is None
                    else tx.page("action", after=scan["action_after"], limit=50)
                )
            )
            for action in action_page["records"]:
                if action["workspace_key"] != self.workspace_key:
                    continue
                try:
                    event = await self._action_event(action)
                    if event:
                        await self._authorize_event(event, None)
                        events.append(event)
                except PersonalAgentError as exc:
                    if exc.code == "storage_error":
                        raise
                    source_errors = True
        return (
            events,
            watches,
            {
                "intent_after": page["next_after"],
                "watch_after": watches_page["next_after"],
                "action_after": action_page["next_after"],
                "partial": bool(
                    page["next_after"]
                    or watches_page["next_after"]
                    or action_page["next_after"]
                    or source_errors
                ),
            },
        )

    async def _read_authority(self, record, actor):
        if record["workspace_key"] != self.workspace_key or record["actor_id"] != actor:
            raise unavailable()
        self._actor(actor)
        for ref in record["event_refs"]:
            await self._authorize_event(await self._event(ref), actor)

    async def authorize_artifact(self, snapshot, actor_id):
        artifacts = getattr(self.engine, "personal_artifacts", None)
        if artifacts is None:
            raise unavailable()

        async def visit(value):
            with _walking(
                self, ("notification-artifact", value["artifact_id"], value["version"], actor_id)
            ) as done:
                if done:
                    return
                evidence = value.get("evidence")
                if not isinstance(evidence, dict):
                    raise self._broken()
                refs = evidence.get("notification_read_refs", [])
                if not isinstance(refs, list) or len(refs) > 64:
                    raise self._broken()
                for ref in refs:
                    keys(
                        ref,
                        {"read_id", "revision", "digest", "workspace_key"},
                        {"read_id", "revision", "digest", "workspace_key"},
                        "原通知读取凭据",
                    )
                    if (
                        type(ref["revision"]) is not int
                        or ref["revision"] != 1
                        or ref["workspace_key"] != self.workspace_key
                        or not isinstance(ref["read_id"], str)
                        or re.fullmatch(r"nx_[a-f0-9]{32}", ref["read_id"]) is None
                    ):
                        raise unavailable()
                    record = await self._record("read", ref["read_id"], revision=1)
                    if (
                        record_digest(record) != ref["digest"]
                        or record["body_sha256"] != value["sha256"]
                    ):
                        raise self._broken()
                    with _walking(self, ("notification-read", record["id"], actor_id)) as read_done:
                        if not read_done:
                            await self._read_authority(record, actor_id)
                for ref in value.get("source_refs", []):
                    parent = await artifacts._call(
                        "get",
                        actor_id=actor_id,
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
                    if parent["sha256"] != ref["sha256"]:
                        raise unavailable()
                    await visit(parent)

        await visit(snapshot)

    async def model_result(self, operation, result, actor, arguments):
        self._actor(actor)
        from naumi_agent.personal.notification_tools import execute_notification_operation

        if operation not in {"context", "list", "preferences", "get-batch", "requests"}:
            raise PersonalAgentError("scope_denied", "模型只能读取自身通知")
        # Rebuild from the query; caller-supplied DTOs are never trusted provenance.
        result = await execute_notification_operation(
            self, json.loads(canonical(arguments)), caller_actor=actor
        )
        refs = []
        if operation == "context":
            result = {
                k: result[k]
                for k in (
                    "workspace_key",
                    "configured",
                    "unread_count",
                    "attention_count",
                    "counts_consistency",
                    "partial",
                    "worker",
                    "quiet",
                )
            }
        elif operation == "preferences":
            result = {
                "revision": result["revision"],
                "digest": result["digest"],
                "enabled": result["preferences"]["enabled"],
                "quiet": result["quiet"],
            }
        elif operation == "requests":
            result = {"status": result["status"], "request_id": result["request_id"]}
        else:
            entries = result["entries"]
            refs = [
                {"event_id": e["id"], "event_digest": e["event_digest"]}
                for e in entries
                if e["availability"] != "source_unavailable"
            ]
            for entry in entries:
                # Human receipt metadata is not model-visible evidence of reading.
                entry.pop("notice_read", None)
        body = json.loads(canonical(result))
        today = self._today()
        session, version = await today._read_session(actor)
        content = canonical(body).encode()
        record = {
            "id": identity("nx_", {"actor": actor, "body": body, "refs": refs}),
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "event_refs": refs,
            "body_sha256": hashlib.sha256(content).hexdigest(),
        }
        await self._read_authority(record, actor)

        def save(tx):
            old = tx.get("read", record["id"], missing=True)
            if old is not None:
                if {k: v for k, v in old.items() if k != "revision"} != record:
                    raise self._broken()
                return old
            return tx.put("read", record, expected_revision=0)

        saved = await self._write(save)
        artifacts = today._artifacts()
        snapshot = await artifacts.publish(
            actor_id=actor,
            request_id="notification-read:" + digest({"read": saved["id"], "body": body}),
            title="通知",
            kind="material",
            filename="notifications.txt",
            content=content,
            source_refs=[],
            evidence={
                "source_kind": "notification_read",
                "notification_read_refs": [
                    {
                        "read_id": saved["id"],
                        "revision": 1,
                        "digest": record_digest(saved),
                        "workspace_key": self.workspace_key,
                    }
                ],
            },
        )
        if snapshot["sha256"] != record["body_sha256"]:
            raise self._broken()
        await artifacts.bind_refs(session.id, actor, [artifacts.reference(snapshot)])
        _, latest = await today._read_session(actor, session)
        if latest != version:
            raise unavailable()
        await self._read_authority(saved, actor)
        return body
