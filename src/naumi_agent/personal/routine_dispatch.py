"""Atomic nominal slots and an idempotent bridge to the original once-Item service."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.item_models import digest as item_digest
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_models import (
    digest,
    fields,
    integer,
    money_microusd,
    normalize_routine_ref,
    occurrence_id,
    slot_identity,
    timestamp,
)
from naumi_agent.personal.routine_schedule import (
    catch_up_occurrences,
    coalesce_occurrences,
    next_occurrences,
)

_ACTIVE = ("reserved", "linked", "unknown")
_TERMINAL = frozenset({"completed", "error", "timeout", "max_turns", "cancelled"})


class RoutineDispatchMixin:
    @staticmethod
    def _reservation_public(tx, row):
        reservation = tx.get("budget", "reservation:" + row["id"], missing=True)
        return {
            "reserved_cost_microusd": 0 if reservation is None else reservation["cost_microusd"],
            "settled_cost_microusd": None
            if reservation is None
            else reservation["known_cost_microusd"],
        }

    async def _occurrence_view(self, row, caller_actor=None):
        routine = await self._routine(row["routine_id"], caller_actor)
        if row["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原发生不在当前工作区")
        value = {
            key: row[key]
            for key in (
                "id",
                "routine_id",
                "epoch",
                "definition_revision",
                "nominal_due_at",
                "local_time",
                "fold",
                "status",
                "item_id",
                "decision",
                "created_at",
            )
        }
        value.update(await self._read(lambda tx: self._reservation_public(tx, row)))
        if caller_actor is None and row["item_id"]:
            value["child"] = await self.items.get(row["item_id"])
        elif caller_actor is not None:
            # Follow actual Item/result tools for body transport and their own authority.
            value.pop("decision", None)
        del routine
        return value

    async def occurrence(self, identity, caller_actor=None):
        occurrence_id(identity)
        return await self._occurrence_view(await self._record("occurrence", identity), caller_actor)

    async def occurrences(self, identity, caller_actor=None, *, limit=50, after=None):
        await self._routine(identity, caller_actor)
        integer(limit, "发生记录页大小", maximum=100)
        if after is not None:
            occurrence_id(after)
        page = await self._read(
            lambda tx: tx.page("occurrence", parent=identity, limit=limit, after=after)
        )
        return {
            "occurrences": [
                await self._occurrence_view(row, caller_actor) for row in page["records"]
            ],
            "next_after": page["next_after"],
        }

    async def _routines_for_worker(self):
        cursor = None
        while True:
            page = await self._read(
                lambda tx: (
                    {"records": [], "next_after": None}
                    if tx is None
                    else tx.page("routine", limit=100, after=cursor)
                )
            )
            for row in page["records"]:
                if row["workspace_key"] == self.workspace_key:
                    yield self._scope(row)
            if page["next_after"] is None:
                break
            cursor = page["next_after"]

    def _slot_row(
        self, routine, slot, *, manual_request=None, decision=None, slot_kind="scheduled"
    ):
        identity, core = slot_identity(
            self.owner_key,
            routine["id"],
            routine["epoch"],
            slot,
            manual_request=manual_request,
            slot_kind=slot_kind,
        )
        return {
            "id": identity,
            "routine_id": routine["id"],
            "workspace_key": self.workspace_key,
            "actor_id": routine["definition"]["assignee"]["id"],
            "epoch": routine["epoch"],
            "definition_revision": routine["revision"],
            "definition_sha256": routine["definition_sha256"],
            "identity": core,
            "identity_sha256": digest(core),
            "nominal_due_at": slot["due_at"],
            "local_time": slot["local_time"],
            "fold": slot["fold"],
            "calendar_evidence": slot.get("calendar_evidence"),
            "status": "skipped" if decision is not None else "reserved",
            "decision": decision,
            "decision_digest": None if decision is None else digest(decision),
            "item_id": None,
            "item_once_key": None,
            "item_definition_sha256": None,
            "item_request_id": "routine-item:" + identity,
            "guard_ref": None,
            "child_input_refs": None,
            "child_payload": None,
            "item_create_attempted": False,
            "created_at": self.stamp(),
            "updated_at": self.stamp(),
        }

    @staticmethod
    def _put_decision(tx, row):
        previous = tx.get("occurrence", row["id"], missing=True)
        if previous is not None:
            if (
                previous["identity_sha256"] != row["identity_sha256"]
                or previous["decision_digest"] != row["decision_digest"]
            ):
                raise PersonalAgentError("binding_conflict", "原发生对应不同的机械决定")
            return previous
        return tx.put("occurrence", row, expected_revision=0)

    async def _mark_attention(self, routine, cursor, code):
        def update(tx):
            current = tx.get("budget", cursor["id"])
            if current["epoch"] != routine["epoch"] or current["revision"] != cursor["revision"]:
                return
            if current.get("attention_code") == code:
                return
            tx.put(
                "budget",
                {**current, "attention_code": code, "updated_at": self.stamp()},
                expected_revision=current["revision"],
            )

        await self._write(update)

    async def _reserve_due(self, routine):
        if routine["status"] != "active":
            return
        cursor = await self._record("budget", "cursor:" + routine["id"])
        if cursor["epoch"] != routine["epoch"]:
            raise self._broken()
        if not cursor["next_due_at"] or timestamp(cursor["next_due_at"]) > self.now():
            return
        schedule, recovery = routine["definition"]["schedule"], routine["definition"]["recovery"]
        now, after = self.now(), timestamp(cursor["cursor_after"])
        decisions, slots, aggregate = [], [], None
        try:
            if recovery["misfire_policy"] == "latest":
                coalesced = coalesce_occurrences(schedule, after, now)
                if coalesced["occurrence"] is not None:
                    slots = [coalesced["occurrence"]]
                    if coalesced["count"] > 1:
                        aggregate = {
                            "kind": "coalesced",
                            "after": after.isoformat(),
                            "through": slots[0]["due_at"],
                            "count": coalesced["count"] - 1,
                            "detail": "按已确认规则仅受理最近一次，早期发生不执行",
                        }
                        decisions.append((slots[0], aggregate, "coalesced"))
            elif recovery["misfire_policy"] == "skip":
                threshold = now - timedelta(seconds=recovery["grace_seconds"], microseconds=1)
                if threshold > after:
                    skipped = coalesce_occurrences(schedule, after, threshold)
                    if skipped["occurrence"] is not None:
                        decision = {
                            "kind": "misfire_skip",
                            "after": after.isoformat(),
                            "through": skipped["occurrence"]["due_at"],
                            "count": skipped["count"],
                            "detail": "超出正常延迟窗口，按规则跳过",
                        }
                        decisions.append((skipped["occurrence"], decision, "skipped_range"))
                        after = timestamp(skipped["occurrence"]["due_at"])
                slots = catch_up_occurrences(schedule, after, now, recovery["max_catch_up"])[
                    "occurrences"
                ]
            else:
                slots = catch_up_occurrences(schedule, after, now, recovery["max_catch_up"])[
                    "occurrences"
                ]
        except PersonalAgentError as exc:
            await self._mark_attention(routine, cursor, exc.code)
            return
        if not decisions and not slots:
            return

        def reserve(tx):
            current = self._scope(tx.get("routine", routine["id"]))
            original = tx.get("budget", cursor["id"])
            if (
                current["status"] != "active"
                or current["revision"] != routine["revision"]
                or original["revision"] != cursor["revision"]
            ):
                return
            limits = current["definition"]["limits"]
            active = tx.list(
                "occurrence",
                parent=current["id"],
                statuses=_ACTIVE,
                limit=10001,
            )
            if len(active) > 10000:
                raise self._broken()
            created, attention = [], None
            pending = {value["id"] for value in active}
            advance = timestamp(original["cursor_after"])
            for slot in slots:
                row = self._slot_row(current, slot)
                previous = tx.get("occurrence", row["id"], missing=True)
                if previous is not None:
                    if previous["identity_sha256"] != row["identity_sha256"]:
                        raise self._broken()
                    created.append(previous)
                    advance = max(advance, timestamp(slot["due_at"]))
                    continue
                if recovery["overlap_policy"] == "skip" and (active or created):
                    row.update(
                        status="skipped",
                        decision={
                            "kind": "overlap_skip",
                            "count": 1,
                            "detail": "前次尚未结束，按规则跳过本次",
                        },
                    )
                    row["decision_digest"] = digest(row["decision"])
                else:
                    if len(pending) >= limits["max_pending"]:
                        attention = "routine_queue_full"
                        break
                    try:
                        self.budget_reserve(tx, current, row["id"])
                    except PersonalAgentError as exc:
                        if exc.code != "budget_exhausted":
                            raise
                        attention = exc.code
                        break
                    pending.add(row["id"])
                created.append(tx.put("occurrence", row, expected_revision=0))
                advance = max(advance, timestamp(slot["due_at"]))
            for slot, decision, kind in decisions:
                # A latest-policy aggregate is committed only together with its
                # selected original slot. Otherwise queue pressure would skip
                # that not-yet-admitted occurrence forever.
                if kind == "coalesced" and not created:
                    continue
                self._put_decision(
                    tx, self._slot_row(current, slot, decision=decision, slot_kind=kind)
                )
                advance = max(advance, timestamp(slot["due_at"]))
            try:
                next_slots = next_occurrences(schedule, advance, 1)
            except PersonalAgentError as exc:
                if exc.code not in {"calendar_coverage_unknown", "calendar_query_limit"}:
                    raise
                next_slots, attention = [], exc.code
            if advance.isoformat() == original["cursor_after"] and attention == original.get(
                "attention_code"
            ):
                return
            tx.put(
                "budget",
                {
                    **original,
                    "cursor_after": advance.isoformat(),
                    "next_due_at": None if not next_slots else next_slots[0]["due_at"],
                    "attention_code": attention,
                    "updated_at": self.stamp(),
                },
                expected_revision=original["revision"],
            )

        await self._write(reserve)

    async def extra_run(self, routine, payload, caller_actor):
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})
        await self.authorize_definition_sources(
            routine["definition"], routine["definition"]["assignee"]["id"]
        )
        now = self.now()
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(routine["definition"]["schedule"]["timezone"]))
        slot = {
            "due_at": now.isoformat(),
            "local_time": local.replace(tzinfo=None).isoformat(),
            "fold": local.fold,
            "calendar_evidence": None,
        }
        semantic = {"routine_id": routine["id"], **payload}

        def reserve(tx):
            current = self._scope(tx.get("routine", routine["id"]), caller_actor)
            if (
                current["revision"] != integer(payload["expected_revision"], "原计划版本")
                or current["status"] != "active"
            ):
                raise PersonalAgentError("version_conflict", "请先核对并启用原计划")
            active = tx.list(
                "occurrence",
                parent=current["id"],
                statuses=_ACTIVE,
                limit=10001,
            )
            if len(active) >= current["definition"]["limits"]["max_pending"]:
                raise PersonalAgentError("routine_queue_full", "本周期待执行数量已满")
            row = self._slot_row(current, slot, manual_request=payload["request_id"])
            self.budget_reserve(tx, current, row["id"])
            return self._put_ref(tx, "occurrence", row)

        ref = await self._mutate(
            payload["request_id"], "extra-run", semantic, caller_actor, reserve
        )
        return await self.occurrence(ref["id"], caller_actor)

    async def _bind_item(self, row, receipt):
        item = receipt["committed_item"]
        expected = self.items._fingerprint("create", "", row["child_payload"], None)
        if (
            receipt["operation"] != "create"
            or receipt["request_fingerprint"] != expected
            or item["definition"]["route"] != self.items._route()
            or item["definition"]["assignee"]["id"] != row["actor_id"]
        ):
            raise PersonalAgentError("binding_conflict", "原周期子事项回执与冻结委托不一致")

        def bind(tx):
            current = tx.get("occurrence", row["id"])
            if current["item_id"] not in {None, item["item_id"]}:
                raise self._broken()
            mapping = tx.get("item_binding", item["item_id"], missing=True)
            if mapping is not None and mapping["occurrence_id"] != row["id"]:
                raise self._broken()
            if mapping is None:
                tx.put(
                    "item_binding",
                    {
                        "id": item["item_id"],
                        "routine_id": row["routine_id"],
                        "occurrence_id": row["id"],
                        "status": "bound",
                        "item_request_id": row["item_request_id"],
                    },
                    expected_revision=0,
                )
            if current["item_id"] is None:
                current = tx.put(
                    "occurrence",
                    {
                        **current,
                        "status": "linked",
                        "item_id": item["item_id"],
                        "item_once_key": item["once_key"],
                        "item_definition_sha256": item["definition_sha256"],
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )
            return current

        return await self._write(bind)

    async def occurrence_for_item(self, item_id):
        mapping = await self._record("item_binding", item_id, missing=True)
        if mapping is not None:
            row = await self._record("occurrence", mapping["occurrence_id"])
            if row["item_id"] != item_id or row["routine_id"] != mapping["routine_id"]:
                raise self._broken()
            return row
        item = await self.items._call("get", item_id=item_id)
        for ref in item["definition"]["inputs"]["artifact_refs"]:
            artifact = await self.engine.personal_artifacts._call(
                "get",
                actor_id=item["definition"]["assignee"]["id"],
                artifact_id=ref["artifact_id"],
                version=ref["version"],
            )
            for raw in artifact.get("evidence", {}).get("routine_refs", []):
                exact = normalize_routine_ref(raw)
                row = await self._record("occurrence", exact["occurrence_id"])
                if (
                    row["routine_id"] != exact["routine_id"]
                    or row["workspace_key"] != self.workspace_key
                ):
                    raise self._broken()
                receipt = await self.items._call("find_request", request_id=row["item_request_id"])
                if receipt is None or receipt["committed_item"]["item_id"] != item_id:
                    raise PersonalAgentError("binding_conflict", "周期资料不授予其他事项执行身份")
                return await self._bind_item(row, receipt)
        return None

    async def _dispatch_occurrence(self, row, routine):
        if row["status"] != "reserved" or row["item_id"] is not None:
            return
        if routine["status"] != "active" or routine["epoch"] != row["epoch"]:
            return
        lease = await self._write(
            lambda tx: tx.claim(
                "bridge", row["id"], self.holder, now=self.now().timestamp(), ttl=300
            )
        )
        try:
            historical = await self._routine(row["routine_id"], revision=row["definition_revision"])
            await self.authorize_definition_sources(historical["definition"], row["actor_id"])
            if row["guard_ref"] is None:
                guard, refs = await self.create_occurrence_guard(row, historical)
                reservation = await self._record("budget", "reservation:" + row["id"])
                definition = historical["definition"]
                payload = {
                    "request_id": row["item_request_id"],
                    "title": definition["title"],
                    "instructions": definition["instructions"],
                    "tags": definition["tags"],
                    "assignee_id": definition["assignee"]["id"],
                    "assignee_version": definition["assignee"]["version"],
                    "input_refs": refs,
                    "directories": definition["inputs"]["directories"],
                    "policy": {
                        **definition["policy"],
                        "max_cost_microusd": reservation["cost_microusd"],
                    },
                    "schedule": {
                        "mode": "now",
                        "timezone": definition["schedule"]["timezone"],
                        "misfire_policy": "run_once",
                        "grace_seconds": 604800,
                    },
                }

                def freeze(tx):
                    tx.fence(
                        "bridge", row["id"], self.holder, lease["fence"], now=self.now().timestamp()
                    )
                    current = tx.get("occurrence", row["id"])
                    if current["guard_ref"] is not None:
                        if current["guard_ref"] != guard or current["child_payload"] != payload:
                            raise self._broken()
                        return current
                    return tx.put(
                        "occurrence",
                        {
                            **current,
                            "guard_ref": guard,
                            "child_input_refs": refs,
                            "child_payload": payload,
                        },
                        expected_revision=current["revision"],
                    )

                row = await self._write(freeze)
            receipt = await self.items._call("find_request", request_id=row["item_request_id"])
            if receipt is None:

                def attempt(tx):
                    tx.fence(
                        "bridge",
                        row["id"],
                        self.holder,
                        lease["fence"],
                        now=self.now().timestamp(),
                        phase="sent",
                    )
                    current = tx.get("occurrence", row["id"])
                    parent = tx.get("routine", row["routine_id"])
                    if (
                        parent["status"] != "active"
                        or parent["epoch"] != current["epoch"]
                        or current["status"] != "reserved"
                    ):
                        raise PersonalAgentError("occurrence_paused", "原周期后续执行已停止")
                    if current["item_create_attempted"]:
                        return current
                    limits = parent["definition"]["limits"]
                    parallel = (
                        limits["max_concurrent"]
                        if parent["definition"]["recovery"]["overlap_policy"] == "allow"
                        else 1
                    )
                    in_flight = sum(
                        bool(
                            other["item_id"]
                            or other["item_create_attempted"]
                            or other["status"] == "unknown"
                        )
                        for other in tx.list(
                            "occurrence",
                            parent=parent["id"],
                            statuses=_ACTIVE,
                            limit=10001,
                        )
                        if other["id"] != current["id"]
                    )
                    if in_flight >= parallel:
                        raise PersonalAgentError(
                            "routine_busy", "原周期执行槽仍被占用，保留本次排队"
                        )
                    return tx.put(
                        "occurrence",
                        {**current, "item_create_attempted": True},
                        expected_revision=current["revision"],
                    )

                row = await self._write(attempt)
                # This is the same local atomic Item request, never a new model
                # request. The original Item DB witness/receipt enforces one row.
                await self.items.create(row["child_payload"])
                receipt = await self.items._call("find_request", request_id=row["item_request_id"])
            if receipt is None:
                raise PersonalAgentError("storage_error", "原子事项创建回执尚未得到持久证明")
            await self._bind_item(row, receipt)
        except PersonalAgentError as exc:
            if exc.code in {
                "storage_error",
                "binding_conflict",
                "routine_busy",
                "version_conflict",
                "lease_lost",
            }:
                raise

            error_code, error_message = exc.code, exc.message

            def block(tx):
                tx.fence(
                    "bridge", row["id"], self.holder, lease["fence"], now=self.now().timestamp()
                )
                current = tx.get("occurrence", row["id"])
                if current["item_id"] is not None:
                    return
                decision = {
                    "kind": "pre_dispatch_error",
                    "code": error_code,
                    "detail": error_message,
                    "count": 1,
                }
                current = tx.put(
                    "occurrence",
                    {
                        **current,
                        "status": "unknown" if current["item_create_attempted"] else "blocked",
                        "decision": decision,
                        "decision_digest": digest(decision),
                    },
                    expected_revision=current["revision"],
                )
                evidence = {
                    "proof_kind": "routine_pre_dispatch",
                    "occurrence_id": current["id"],
                    "decision_sha256": current["decision_digest"],
                }
                self.budget_settle(
                    tx,
                    current,
                    0 if not current["item_create_attempted"] else None,
                    evidence,
                    False,
                )

            await self._write(block)
        finally:
            try:
                await self._write(
                    lambda tx: tx.release(
                        "bridge",
                        row["id"],
                        self.holder,
                        lease["fence"],
                        now=self.now().timestamp(),
                    )
                )
            except PersonalAgentError as exc:
                # An expired bridge may not release a successor's lease. Do not
                # mask the actual dispatch failure or mutate the old occurrence.
                if exc.code != "lease_lost":
                    raise

    async def _reconcile_unlinked(self, row, routine):
        if row["item_create_attempted"]:
            receipt = await self.items._call("find_request", request_id=row["item_request_id"])
            if receipt is not None:
                return await self._bind_item(row, receipt)
        if routine["status"] == "active" and routine["epoch"] == row["epoch"]:
            return row

        def stop(tx):
            current = tx.get("occurrence", row["id"])
            parent = tx.get("routine", row["routine_id"])
            if current["item_id"] is not None or (
                parent["status"] == "active" and parent["epoch"] == current["epoch"]
            ):
                return current
            if current["status"] not in _ACTIVE:
                return current
            attempted = current["item_create_attempted"]
            decision = {
                "kind": "pre_dispatch_stopped",
                "count": 1,
                "parent_revision": parent["revision"],
                "detail": "原周期已暂停、结束或更改日历，未开始的发生不再执行",
            }
            if current["decision"] != decision:
                current = tx.put(
                    "occurrence",
                    {
                        **current,
                        "status": "unknown" if attempted else "skipped",
                        "decision": decision,
                        "decision_digest": digest(decision),
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )
            self.budget_settle(
                tx,
                current,
                None if attempted else 0,
                {
                    "proof_kind": "routine_pre_dispatch",
                    "occurrence_id": current["id"],
                    "decision_sha256": current["decision_digest"],
                },
                False,
            )
            return current

        return await self._write(stop)

    async def _terminal_cost(self, row, intent, job):
        """Read authenticated internal receipts, independently of redacted UI bodies."""
        evidence = {
            "item_id": row["item_id"],
            "occurrence_id": row["id"],
            "job_id": job.job_id,
            "request_sha256": job.request_sha256,
            "lifecycle_receipt": asdict(job.latest_receipt),
        }
        if job.result is None:
            prestart = job.state is AgentJobState.CANCELLED and job.latest_receipt.reason_code in {
                "agent_job_cancelled_before_claim",
                "agent_job_cancelled_before_start",
            }
            return (
                0 if prestart else None,
                evidence,
                job.state in {AgentJobState.RUNNING, AgentJobState.UNKNOWN},
            )
        result = job.result
        payload = await self.items.executor.jobs.recover_terminal_payload(
            job.job_id,
            expected_result_sha256=result.result_sha256,
        )
        manifest = strict_json_loads(payload.response)
        run_id = intent.get("references", {}).get("run_id", "")
        if (
            manifest.get("type") != "personal_item_result"
            or manifest.get("version") != 1
            or manifest.get("item_id") != row["item_id"]
            or manifest.get("occurrence_id") != intent["occurrence_id"]
            or manifest.get("run_id") != run_id
            or result.request_sha256 != job.request_sha256
        ):
            raise PersonalAgentError("binding_conflict", "周期费用缺少原子事项终态绑定")
        run = await self.engine.chat_run_store.get_run(intent["execution_session_id"], run_id)
        if (
            run is None
            or run.receipt is None
            or run.receipt.receipt_id != manifest.get("receipt_id")
            or item_digest(run.receipt.to_dict()) != manifest.get("receipt_sha256")
        ):
            raise PersonalAgentError("storage_error", "周期费用的原 Run 与认证回执缺失")
        evidence.update(
            run_id=run_id,
            result_sha256=result.result_sha256,
            manifest_sha256=digest(manifest),
            receipt_sha256=manifest["receipt_sha256"],
        )
        known = None
        if manifest.get("usage_known") is True and isinstance(manifest.get("usage"), dict):
            actual = None if run.usage is None else run.usage.model_dump(mode="json")
            if actual is None or actual != manifest["usage"]:
                raise PersonalAgentError("binding_conflict", "周期费用与原 Run 用量不一致")
            cost = money_microusd(actual.get("reported_cost_usd"))
            # Worker contracts round to microdollars; reserve/settle round up.
            # Preserve the conservative estimate without mistaking rounding for
            # an entirely different execution receipt.
            if abs(cost - result.total_cost_microusd) > 1:
                raise PersonalAgentError("binding_conflict", "周期费用与认证 Job 结果不一致")
            known = max(cost, result.total_cost_microusd)
        return known, evidence, True

    async def _reconcile_occurrence(self, row, routine):
        if row["item_id"] is None:
            row = await self._reconcile_unlinked(row, routine)
            if row["item_id"] is None:
                return None
        child = await self.items.get(row["item_id"])
        if (
            child["once_key"] != row["item_once_key"]
            or child["definition_sha256"] != row["item_definition_sha256"]
        ):
            raise PersonalAgentError("binding_conflict", "原周期子事项被另一次委托替换")
        intent = await self.items._current_intent(child)
        job = None
        if intent:
            from naumi_agent.daemons.agent_jobs import agent_job_id_for_request
            from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest

            raw = intent["request"]
            request = AgentWorkerRequest(**{**raw, "tool_scope": tuple(raw["tool_scope"])})
            job = await self.items.executor.jobs.get(agent_job_id_for_request(request))
            if job is not None and job.request != request:
                raise self._broken()
        running = (
            job is not None
            and job.state is AgentJobState.RUNNING
            and bool(job.claim_owner_id)
            and job.claim_epoch > 0
            and job.claim_expires_at is not None
            and timestamp(job.claim_expires_at) > self.now()
        )
        stale = routine["epoch"] != row["epoch"]
        stopped = routine["status"] in {"paused", "cancelled", "closed"}
        if (
            (stale or stopped) and not running or routine["status"] in {"cancelled", "closed"}
        ) and child["state"] == "active":
            try:
                child = await self.items.control(
                    row["item_id"],
                    "cancel",
                    {
                        "request_id": "routine-stop:" + row["id"] + ":" + str(routine["revision"]),
                        "expected_revision": child["revision"],
                    },
                )
            except PersonalAgentError as exc:
                if exc.code != "version_conflict":
                    raise
                child = await self.items.get(row["item_id"])
        view = child.get("occurrence")
        if view is None:
            if child["state"] not in {"cancelled", "closed"}:
                return child
            # A cancelled local Item without a Job receipt is not equivalent to
            # a proven zero-cost admission. Surface its unresolved hold instead
            # of leaving the occurrence deceptively linked forever.
            state = "cancelled"
        else:
            state = job.state.value if job is not None else view["status"]
        started = job is not None and (
            job.state in {AgentJobState.RUNNING, AgentJobState.UNKNOWN} or job.result is not None
        )
        if started:
            await self._write(lambda tx: self.budget_started(tx, row))
        if state not in _TERMINAL | {"unknown", "skipped"}:
            return child
        known, evidence = None, {"item_id": row["item_id"], "occurrence_id": row["id"]}
        if job is not None:
            known, evidence, terminal_started = await self._terminal_cost(row, intent, job)
            started = started or terminal_started

        def settle(tx):
            current = tx.get("occurrence", row["id"])
            reservation = self.budget_settle(tx, current, known, evidence, bool(started))
            status = (
                "settled"
                if reservation["settlement"] in {"known", "prestart_cancelled"}
                else "unknown"
            )
            if current["status"] == status:
                return current
            return tx.put(
                "occurrence",
                {**current, "status": status, "updated_at": self.stamp()},
                expected_revision=current["revision"],
            )

        await self._write(settle)
        return child

    async def tick(self):
        if self._closing:
            return
        async with self._tick_lock:
            async for routine in self._routines_for_worker():
                try:
                    rows, after = [], None
                    while True:
                        page = await self._read(
                            lambda tx: tx.page(
                                "occurrence",
                                parent=routine["id"],
                                statuses=_ACTIVE,
                                limit=100,
                                after=after,
                            )
                        )
                        rows.extend(page["records"])
                        if page["next_after"] is None:
                            break
                        after = page["next_after"]
                    rows.sort(key=lambda row: (row["nominal_due_at"], row["id"]))
                    active = []
                    for row in rows:
                        child = await self._reconcile_occurrence(row, routine)
                        current = await self._record("occurrence", row["id"])
                        if current["status"] in _ACTIVE:
                            active.append((current, child))
                    recovery = routine["definition"]["recovery"]
                    maximum = routine["definition"]["limits"]["max_concurrent"]
                    in_flight = len(
                        [row for row, _ in active if row["item_id"] or row["status"] == "unknown"]
                    )
                    for row, _ in active:
                        if row["item_id"] is not None or row["status"] != "reserved":
                            continue
                        if (
                            recovery["overlap_policy"] != "allow"
                            and in_flight
                            or in_flight >= maximum
                        ):
                            break
                        await self._dispatch_occurrence(row, routine)
                        if (await self._record("occurrence", row["id"]))["status"] == "linked":
                            in_flight += 1
                    # Dispatch existing reservations before adding the next batch.
                    await self._reserve_due(await self._routine(routine["id"]))
                except PersonalAgentError as exc:
                    if exc.code == "storage_error":
                        raise
                    cursor = await self._record("budget", "cursor:" + routine["id"], missing=True)
                    if cursor is not None:
                        await self._mark_attention(routine, cursor, exc.code)
