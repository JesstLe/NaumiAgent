"""Deterministic original-deadline and committed-calendar-change conditions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from naumi_agent.personal.followup_models import error
from naumi_agent.personal.item_models import digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_schedule import _zone
from naumi_agent.personal.today_models import (
    semantic_item_ref,
    validate_deadline,
)


class FollowupSourcesMixin:
    async def _source_binding(self, body):
        actor = body["actor_id"]
        if body["trigger"]["kind"] == "calendar_change":
            head = await self.connections.enroll_changes(body["trigger"]["connection_id"], actor)
            return {
                "kind": "calendar_change",
                "connection_id": body["trigger"]["connection_id"],
                **head,
            }
        await self.items._call("ensure_identity")
        self.today.personal.store.list()
        await asyncio.to_thread(self.today._guard)
        await asyncio.to_thread(self.today.store.write, lambda tx: None)
        await asyncio.to_thread(self.today._guard)
        return {
            "kind": "deadline",
            "route": self.items._route(),
            "item_instance": await self.items._call("identity"),
            "today_instance": await asyncio.to_thread(self.today.store.identity),
        }

    async def _binding_authority(self, rule, *, fresh=False):
        actor = rule["body"]["actor_id"]
        profile = self._actor(actor)
        if (
            profile["version"] != rule["body"]["actor_version"]
            or digest(profile) != rule["actor_digest"]
        ):
            raise error("source_stale")
        if _zone(rule["body"]["timezone"])[1] != rule["timezone_sha256"]:
            raise error("timezone_data_changed")
        binding = rule["source_binding"]
        if binding["kind"] == "deadline":
            await asyncio.to_thread(self.today._guard)
            if (
                binding["route"] != self.items._route()
                or binding["item_instance"] != await self.items._call("identity")
                or binding["today_instance"] != await asyncio.to_thread(self.today.store.identity)
            ):
                raise error("source_stale")
            return binding
        current = await self.connections.change_head(binding["connection_id"], actor)
        for key in ("connection_instance", "auth_epoch", "grant"):
            if current[key] != binding[key]:
                raise error("source_stale")
        if fresh:
            stamp = current["last_sync_at"]
            if not stamp or self.now() - datetime.fromisoformat(stamp) > timedelta(
                seconds=rule["body"]["freshness_seconds"]
            ):
                raise error("calendar_stale")
            if self.now() < datetime.fromisoformat(stamp):
                raise error("clock_rollback")
        return current

    def _stage(self, rule, deadline):
        validate_deadline(deadline, allow_timezone_change=False)
        boundary = datetime.fromisoformat(
            deadline["boundary_at"] if deadline["kind"] == "date" else deadline["resolved_at"]
        )
        stages = [
            (boundary - timedelta(minutes=offset), offset)
            for offset in rule["body"]["trigger"]["offsets_minutes"]
        ]
        eligible = [(stamp, offset) for stamp, offset in stages if stamp <= self.now()]
        if not eligible:
            return None
        nominal, offset = max(eligible)
        age = (self.now() - nominal).total_seconds()
        maximum = (
            60
            if rule["body"]["misfire_policy"] == "skip"
            else rule["body"]["max_lateness_minutes"] * 60
        )
        if age > maximum or (
            not rule["body"]["include_existing"]
            and nominal < datetime.fromisoformat(rule["cutover_at"])
        ):
            return None
        return nominal, offset, maximum

    async def _deadline_candidate(self, rule, metadata):
        self.today._metadata_scope(metadata)
        actor = rule["body"]["actor_id"]
        chosen = rule["body"]["trigger"]["item_ids"]
        if chosen and metadata["item_ref"]["item_id"] not in chosen:
            return None
        try:
            await self.items._item(metadata["item_ref"]["item_id"], actor)
        except PersonalAgentError as exc:
            if exc.code == "not_found":
                return None
            raise
        if (
            metadata["status"] != "active"
            or metadata["deadline"] is None
            or (metadata["human_check"] and metadata["human_check"]["checked"])
        ):
            return None
        await self.today._authorize_metadata_sources(metadata, actor)
        original, current = await self.today._authorize_item(
            metadata["item_ref"], actor, require_current=True
        )
        if current["state"] in {"closed", "cancelled"}:
            return None
        stage = self._stage(rule, metadata["deadline"])
        if stage is None:
            return None
        nominal, offset, maximum = stage
        source = {
            "kind": "deadline",
            "item_ref": metadata["item_ref"],
            "metadata_ref": self.today._metadata_reference(metadata),
            "deadline": metadata["deadline"],
        }
        key = {
            "rule_id": rule["id"],
            "epoch": rule["epoch"],
            "item": semantic_item_ref(metadata["item_ref"]),
            "deadline": metadata["deadline"],
            "offset": offset,
        }
        return {
            "key": key,
            "source": source,
            "nominal_at": nominal.isoformat(timespec="microseconds"),
            "expires_at": (nominal + timedelta(seconds=maximum)).isoformat(timespec="microseconds"),
            "reason_code": "deadline_approaching"
            if offset > 0
            else "deadline_due"
            if offset == 0
            else "deadline_overdue",
            "offset_minutes": offset,
            "effect_kind": "today_place",
        }

    async def _calendar_candidate(self, rule, change):
        actor = rule["body"]["actor_id"]
        trigger = rule["body"]["trigger"]
        if trigger["event_id"] and change["after_ref"]["event_id"] != trigger["event_id"]:
            return None
        kind = change["change_kind"]
        if kind == "baseline":
            if not rule["body"]["include_existing"]:
                return None
        elif kind not in trigger["change_kinds"]:
            return None
        if kind == "updated" and not set(change["changed_fields"]) & set(trigger["fields"]):
            return None
        connection = await self.connections._connection(trigger["connection_id"], actor)
        raw_ref = self.connections.change_reference(change, connection, actor)
        value = await self.connections.capture_change(raw_ref, actor, require_current=True)
        # A series master is not an occurrence. Existing sync expands real instances.
        if value["after"] and value["after"].get("recurrence"):
            return None
        nominal = datetime.fromisoformat(change["observed_at"])
        maximum = (
            60
            if rule["body"]["misfire_policy"] == "skip"
            else rule["body"]["max_lateness_minutes"] * 60
        )
        if self.now() < nominal or (self.now() - nominal).total_seconds() > maximum:
            return None
        current = value["current"]
        tombstone = not current.get("available", True) or current["status"] == "cancelled"
        return {
            "key": {"rule_id": rule["id"], "epoch": rule["epoch"], "change_ref": raw_ref},
            "source": {"kind": "calendar_change", "change_ref": raw_ref, "tombstone": tombstone},
            "nominal_at": nominal.isoformat(timespec="microseconds"),
            "expires_at": (nominal + timedelta(seconds=maximum)).isoformat(timespec="microseconds"),
            "reason_code": "calendar_existing"
            if kind == "baseline"
            else "calendar_cancelled"
            if kind == "cancelled"
            else "calendar_unavailable"
            if kind == "unavailable"
            else "calendar_restored"
            if kind == "restored"
            else "calendar_changed",
            "effect_kind": "calendar_draft",
        }

    async def _source_page(self, rule, scan):
        await self._binding_authority(rule, fresh=True)
        candidates, errors = [], False
        if rule["body"]["trigger"]["kind"] == "deadline":
            page = await self.today._read(
                lambda tx: (
                    {"records": [], "next_after": None}
                    if tx is None
                    else tx.page("metadata", after=scan.get("after"), limit=50)
                )
            )
            for metadata in page["records"]:
                if metadata["workspace_key"] != self.workspace_key:
                    continue
                try:
                    candidate = await self._deadline_candidate(rule, metadata)
                    if candidate:
                        candidates.append(candidate)
                except Exception as exc:
                    from naumi_agent.personal.models import PersonalAgentError

                    if not isinstance(exc, PersonalAgentError) or exc.code == "storage_error":
                        raise
                    errors = True
            return candidates, {
                "after": page["next_after"],
                "partial": page["next_after"] is not None or errors,
            }
        page = await self.connections.change_page(
            rule["body"]["trigger"]["connection_id"],
            rule["body"]["actor_id"],
            after_sequence=scan.get(
                "sequence",
                0 if rule["body"]["include_existing"] else rule["source_binding"]["head"],
            ),
            limit=50,
        )
        for change in page["changes"]:
            try:
                candidate = await self._calendar_candidate(rule, change)
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                from naumi_agent.personal.models import PersonalAgentError

                if not isinstance(exc, PersonalAgentError) or exc.code == "storage_error":
                    raise
                # Superseded history is cursor-accounted; never adopted as current.
                errors = errors or exc.code not in {"source_stale", "version_conflict"}
        return candidates, {"sequence": page["next_sequence"], "partial": page["partial"] or errors}

    async def _proposal_source(self, proposal, rule, *, current=True, fresh=True):
        actor, source = proposal["actor_id"], proposal["source"]
        await self._binding_authority(rule, fresh=fresh)
        if source["kind"] == "deadline":
            old, original, actual = await self.today._authorized_metadata_source(
                source["metadata_ref"], actor,
                expected_item_ref=source["item_ref"], require_current=current,
            )
            if old["deadline"] != source["deadline"]:
                raise error("storage_error")
            metadata = await self.today._metadata_for(source["item_ref"])
            if current:
                await self.today._authorize_metadata_sources(metadata, actor)
                if (
                    metadata["deadline"] != source["deadline"]
                    or metadata["status"] != "active"
                    or actual["state"] in {"closed", "cancelled"}
                    or metadata["human_check"]
                    and metadata["human_check"]["checked"]
                ):
                    raise error("source_stale")
                stage = self._stage(rule, metadata["deadline"])
                if stage is None or stage[1] != proposal["offset_minutes"]:
                    raise error("source_stale")
            return original["definition"]["title"], {
                "kind": "deadline",
                "item_ref": source["item_ref"],
                "metadata_ref": source["metadata_ref"],
                "current_metadata_revision": metadata["revision"],
                "deadline": source["deadline"],
            }
        value = await self.connections.capture_change(
            source["change_ref"], actor, require_current=current
        )
        actual = value["current"]
        if not source["tombstone"] and (
            not actual.get("available", True) or actual["status"] == "cancelled"
        ):
            raise error()
        after = value["after"]
        title = (
            ((after or {}).get("summary") or "日程变更")
            if not source["tombstone"]
            else "核对日程状态变化"
        )
        change = value["change"]
        return title, {
            "kind": "calendar_change",
            "change_ref": source["change_ref"],
            "change_kind": change["change_kind"],
            "changed_fields": change["changed_fields"],
            "before": value["before"],
            "after": after,
            "last_sync_at": value["last_sync_at"],
        }
