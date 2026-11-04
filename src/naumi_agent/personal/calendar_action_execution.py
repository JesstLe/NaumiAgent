"""Deterministic calendar execution using existing AgentJob, Session, Run and inbox."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime

from naumi_agent.daemons.agent_jobs import (
    TERMINAL_AGENT_JOB_STATES,
    AgentJobCapacityExhaustedError,
    AgentJobLifecycleConflictError,
    AgentJobPayload,
    AgentJobPublicationState,
    AgentJobState,
    AgentJobTerminalPayload,
    agent_job_id_for_request,
)
from naumi_agent.daemons.agent_worker_contract import (
    AgentWorkerRequest,
    issue_agent_worker_request,
    issue_agent_worker_result,
)
from naumi_agent.memory.session import Session
from naumi_agent.personal.connection_models import canonical, digest, timestamp
from naumi_agent.personal.google_calendar import normalize_event
from naumi_agent.personal.google_oauth import GoogleProtocolError
from naumi_agent.personal.google_oauth import _text as provider_text
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runs.models import CompletionReceipt, ReceiptApproval


def build_action_intent(service, action):
    suffix = action["id"][3:]
    session_id, run_id = "pca_" + suffix, "calendar-run-" + suffix
    payload = AgentJobPayload(
        task_id=action["id"],
        session_id=session_id,
        task="执行用户已确切批准的日历行动；不调用模型",
        context=canonical(
            {
                "type": "personal_calendar_action",
                "version": 1,
                "workspace_key": service.workspace_key,
                "action_id": action["id"],
                "digest": action["digest"],
                "approval_receipt_id": action["approval"]["receipt_id"],
                "artifact_refs": action["artifact_refs"],
                "memory_refs": action["memory_refs"],
                "memory_item_id": action["memory_item_id"],
            }
        ),
        message_topic="personal-calendar:" + action["id"],
    )
    request = issue_agent_worker_request(
        **asdict(payload),
        agent_name=action["actor_id"],
        tool_scope=(),
        permission_mode=str(service.engine.config.safety.permission_mode),
        model_tier="fast",
        max_turns=1,
        max_budget_usd=0,
        timeout_seconds=60,
        issued_at=action["approval"]["approved_at"],
    )
    value = {
        "namespace": "personal-engine-v1:" + service.workspace_key,
        "session_id": session_id,
        "run_id": run_id,
        "request": {**asdict(request), "tool_scope": list(request.tool_scope)},
        "payload": asdict(payload),
        "job_id": agent_job_id_for_request(request),
    }
    value["prepared_sha256"] = digest(value)
    return {**value, "admission": "prepared", "job_bound": False}


def action_request(intent):
    raw = intent["request"]
    return AgentWorkerRequest(**{**raw, "tool_scope": tuple(raw["tool_scope"])})


def _run_status(action_status):
    if action_status == "verified":
        return "completed"
    if action_status == "unknown":
        return "unknown"
    if action_status in {"cancelled", "rejected", "expired"}:
        return "cancelled"
    return "error"


class _UpdatePreflightError(PersonalAgentError):
    """A current update has not entered PATCH; previous dispatch facts remain untouched."""


class CalendarActionExecution:
    def __init__(self, service):
        self.service = service
        self.engine = service.engine
        self.jobs = self.engine.personal_collaborations.jobs
        self.owner = service.holder
        self.active = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    def _intent(self, value):
        intent = value["execution"]
        frozen = {
            key: item
            for key, item in intent.items()
            if key not in {"prepared_sha256", "admission", "job_bound"}
        }
        if (
            digest(frozen) != intent["prepared_sha256"]
            or intent["namespace"] != "personal-engine-v1:" + self.service.workspace_key
            or action_request(intent).agent_name != value["actor_id"]
            or agent_job_id_for_request(action_request(intent)) != intent["job_id"]
        ):
            raise PersonalAgentError("binding_conflict", "日历行动执行意图与原件不一致")
        return intent

    async def _save(self, identity, changes):
        def save(tx):
            current = tx.get("action", identity)
            update = {**changes, "updated_at": self.service.stamp()}
            if current["status"] == "verified" and update.get("status", "verified") != "verified":
                update.pop("status", None)
                update.pop("result", None)
            return tx.put("action", {**current, **update}, expected_revision=current["revision"])

        return await self.service._write(save)

    async def _observe_finished(self):
        for identity, task in list(self.active.items()):
            if not task.done():
                continue
            try:
                task.result()
            except BaseException as exc:
                # Keep the task until this observation is persisted; failed storage
                # is retried by the next tick instead of silently discarding it.
                current = await self.service._record("action", identity)
                await self._save(
                    identity,
                    {
                        "status": "unknown"
                        if current.get("dispatch")
                        and current["status"] not in {"verified", "conflict"}
                        else current["status"],
                        "execution_observation": {
                            "error_code": "background_execution_error",
                            "exception_type": type(exc).__name__,
                            "observed_at": self.service.stamp(),
                        },
                        "last_error_code": "background_execution_error",
                    },
                )
            del self.active[identity]

    async def tick(self):
        async with self._lock:
            await self._observe_finished()
            if self._stopping:
                return
            values = await self.service._read(
                lambda tx: [] if tx is None else tx.list("action", limit=1001)
            )
            if len(values) > 1000:
                raise PersonalAgentError("invalid_input", "日历行动超过本机执行上限")
            for value in values:
                if (
                    value["workspace_key"] != self.service.workspace_key
                    or value["execution"] is None
                    or value["id"] in self.active
                ):
                    continue
                intent = self._intent(value)
                job = await self.jobs.get(intent["job_id"])
                if job is not None and job.request != action_request(intent):
                    raise PersonalAgentError("binding_conflict", "日历 Job 原件与行动不一致")
                if (
                    job is not None
                    and job.state is AgentJobState.RUNNING
                    and timestamp(job.claim_expires_at) <= self.service.now()
                ):
                    await self._recover_interrupted(value, job)
                elif value["status"] in {"dispatching", "submitted"}:
                    await self._recover_interrupted(value, job)
                elif value["status"] == "approved":
                    await self._admit(value, job)
                elif (
                    job is not None
                    and job.state in TERMINAL_AGENT_JOB_STATES
                    and job.result is not None
                ):
                    await self._deliver(value, job)
                elif value["status"] in {"rejected", "expired", "cancelled"} and job is not None:
                    if job.state is AgentJobState.ADMITTED:
                        await self.jobs.cancel_before_claim(job.job_id)

    async def _admit(self, value, job):
        try:
            lease = await self.service._claim("action_admit", value["id"])
        except PersonalAgentError as exc:
            if exc.code == "connection_busy":
                return
            raise
        try:
            value = await self.service._record("action", value["id"])
            intent = self._intent(value)
            if value["status"] != "approved":
                return
            job = await self.jobs.get(intent["job_id"])
            if job is None and intent["admission"] != "prepared":
                await self._save(
                    value["id"], {"status": "unknown", "last_error_code": "admission_unknown"}
                )
                return
            if job is not None and job.request != action_request(intent):
                raise PersonalAgentError("binding_conflict", "原日历 Job 与批准意图不一致")
            if job is not None and job.state is AgentJobState.RUNNING:
                await self._recover_interrupted(value, job)
                return
            if job is not None and job.state in TERMINAL_AGENT_JOB_STATES:
                if job.result is not None:
                    await self._deliver(value, job)
                else:
                    await self._save(
                        value["id"],
                        {"status": "unknown", "last_error_code": "job_terminal_unknown"},
                    )
                return
            try:
                await self.service._authorize_action(value)
            except PersonalAgentError as exc:
                if exc.code in {"storage_error", "connection_busy", "lease_lost"}:
                    raise
                error_code = exc.code

                def cancel_unstarted(tx):
                    current = tx.get("action", value["id"])
                    if current["status"] != "approved" or current.get("dispatch"):
                        return current
                    return tx.put(
                        "action",
                        {
                            **current,
                            "status": "expired" if error_code == "action_expired" else "cancelled",
                            "last_error_code": error_code,
                        },
                        expected_revision=current["revision"],
                    )

                await self.service._write(cancel_unstarted)
                if job is not None and job.state is AgentJobState.ADMITTED:
                    await self.jobs.cancel_before_claim(job.job_id)
                return
            if job is None:

                def attempting(tx):
                    self.service._fence(tx, "action_admit", value["id"], lease)
                    current = tx.get("action", value["id"])
                    if (
                        current["status"] != "approved"
                        or current["execution"]["admission"] != "prepared"
                    ):
                        raise PersonalAgentError("version_conflict", "原日历行动受理状态已变化")
                    self.service._action_current(tx, current, approved=True)
                    return tx.put(
                        "action",
                        {
                            **current,
                            "execution": {**current["execution"], "admission": "attempted"},
                        },
                        expected_revision=current["revision"],
                    )

                value = await self.service._write(attempting)
                try:
                    transition = await self.jobs.admit_for_capacity(
                        request=action_request(intent),
                        payload=AgentJobPayload(**intent["payload"]),
                        owner_id=self.owner,
                        lease_seconds=90,
                        max_active_jobs=self.engine.config.safety.max_parallel_agents,
                        max_waiters=self.engine.config.safety.max_queued_agents,
                    )
                except AgentJobCapacityExhaustedError:
                    await self._save(
                        value["id"], {"execution": {**value["execution"], "admission": "prepared"}}
                    )
                    return
                job = transition.job
            else:
                transition = await self.jobs.claim_for_capacity(
                    job.job_id,
                    owner_id=self.owner,
                    lease_seconds=90,
                    max_active_jobs=self.engine.config.safety.max_parallel_agents,
                    max_waiters=self.engine.config.safety.max_queued_agents,
                )
                job = transition.job
            value = await self._save(
                value["id"],
                {
                    "job_id": job.job_id,
                    "execution": {**value["execution"], "admission": "bound", "job_bound": True},
                },
            )
            if transition.should_dispatch:
                self.active[value["id"]] = asyncio.create_task(
                    self._run(value, job), name="calendar-action:" + value["id"]
                )
        except AgentJobLifecycleConflictError:
            return
        finally:
            await self.service._write(
                lambda tx: self.service._fence(
                    tx, "action_admit", value["id"], lease, phase="finished"
                )
            )

    async def _authorize_job(self, value, job):
        await self.service._authorize_action(value)
        current = await self.jobs.get(job.job_id)
        if (
            current is None
            or current.state is not AgentJobState.RUNNING
            or current.claim_owner_id != self.owner
            or current.claim_epoch != job.claim_epoch
            or current.request != job.request
            or timestamp(current.claim_expires_at) <= self.service.now()
        ):
            raise PersonalAgentError("execution_fenced", "日历行动执行租约已失效")
        return current

    async def _renew(self, value, job, execution):
        try:
            while True:
                await asyncio.sleep(20)
                await self.jobs.renew_claim(
                    job.job_id, owner_id=self.owner, claim_epoch=job.claim_epoch, lease_seconds=90
                )
                await self._authorize_job(value, job)
        except asyncio.CancelledError:
            raise
        except BaseException:
            execution.cancel()
            raise

    async def _session_run(self, value):
        intent = self._intent(value)
        marker = "" if value["actor_id"] == "main" else value["actor_id"]
        if marker:
            self.service.personal.store.register_execution_session(
                marker,
                self.service.workspace_key,
                intent["session_id"],
                session_db_key=self.service.personal.session_db_key,
            )
        session = await self.engine.session_store.load(intent["session_id"])
        if session is None:
            session = Session(
                id=intent["session_id"],
                title="日历行动",
                model="deterministic-calendar-protocol",
                workspace_root=str(self.engine.workspace_root),
                personal_agent_id=marker,
                personal_execution_id=value["id"],
            )
            await self.engine.session_store.save(session)
        elif (
            session.personal_execution_id != value["id"]
            or session.personal_agent_id != marker
            or session.messages
        ):
            raise PersonalAgentError("binding_conflict", "日历行动的独立会话原件已变化")
        if (
            await self.engine.chat_run_store.get_run(intent["session_id"], intent["run_id"])
            is not None
        ):
            raise PersonalAgentError("execution_unknown", "原日历 Run 已存在，未重新执行")
        await self.engine.chat_run_store.start_run(
            session_id=intent["session_id"],
            user_message_id=value["approval"]["receipt_id"],
            run_id=intent["run_id"],
        )
        await self._save(value["id"], {"run_id": intent["run_id"]})

    async def _preflight_update(self, value, token, job):
        proof = {
            "phase": "before_patch",
            "status": "checking",
            "event_id": value["event_id"],
            "base_etag": value["base_etag"],
            "job_id": job.job_id,
            "claim_epoch": job.claim_epoch,
            "request_sha256": job.request_sha256,
            "checked_at": self.service.stamp(),
        }
        await self._save(value["id"], {"preflight": proof})
        code, provider_code, raw_sha = None, None, None
        try:
            remote = await self.service.adapter.get_event(token, value["event_id"])
            if not isinstance(remote, dict) or remote.get("id") != value["event_id"]:
                raise ValueError()
            provider_text(remote.get("etag"), 512)
            raw_sha = digest(remote)
            restricted = any(
                remote.get(name) not in (None, [], {})
                for name in (
                    "attendees",
                    "attachments",
                    "conferenceData",
                    "hangoutLink",
                    "recurrence",
                    "recurringEventId",
                )
            )
            if (
                remote.get("status") == "cancelled"
                or restricted
                or remote.get("eventType", "default") != "default"
            ):
                code = "preflight_unsupported_target"
            elif remote["etag"] != value["base_etag"]:
                code = "preflight_etag_conflict"
            else:
                normalize_event(remote)
        except GoogleProtocolError as exc:
            code, provider_code = "preflight_read_failed", exc.code
        except (ValueError, TypeError, PersonalAgentError):
            code = "preflight_read_failed"
        proof.update(
            status="passed" if code is None else "blocked",
            raw_sha256=raw_sha,
            error_code=code,
            provider_error_code=provider_code,
            checked_at=self.service.stamp(),
        )
        await self._save(value["id"], {"preflight": proof})
        await self.engine.chat_run_store.append_step(
            value["execution"]["run_id"],
            sequence=2,
            stage="calendar_update_preflight",
            status="completed" if code is None else "failed",
            summary="原对象已通过普通日程与 ETag 写前检查"
            if code is None
            else "原对象未通过写前检查，本次没有发送 PATCH",
            metadata={
                "event_id": value["event_id"],
                "error_code": code,
                "raw_sha256": raw_sha,
                "model_calls": 0,
                "phase": "before_patch",
            },
        )
        if code is not None:
            raise _UpdatePreflightError(code, "原日历对象尚未通过写前核验，本次未修改")

    async def _run(self, value, job):
        renewal = None
        status, error = "unknown", None
        started = False
        try:
            await self.service._authorize_action(value)
            payload = await self.jobs.recover_payload(
                job.job_id, owner_id=self.owner, claim_epoch=job.claim_epoch
            )
            if payload != AgentJobPayload(**value["execution"]["payload"]):
                raise PersonalAgentError("binding_conflict", "日历 Job 正文与原意图不一致")
            await self._session_run(value)
            await self.jobs.mark_running(
                job.job_id, owner_id=self.owner, claim_epoch=job.claim_epoch
            )
            started = True
            renewal = asyncio.create_task(self._renew(value, job, asyncio.current_task()))
            await self.engine.chat_run_store.append_step(
                value["execution"]["run_id"],
                sequence=1,
                stage="calendar_authorization",
                status="completed",
                summary="已核对确切批准、原账户和执行租约",
                metadata={"action_id": value["id"], "digest": value["digest"], "model_calls": 0},
            )
            async with asyncio.timeout(60):
                credential = await self.service.token_snapshot(
                    value["connection_id"], value["actor_id"], write=True
                )
                token = credential.access_token
                await self._authorize_job(value, job)
                if value["operation"] == "update":
                    await self._preflight_update(value, token, job)
                    await self._authorize_job(value, job)
                await self.service.assert_token_snapshot(credential, value["actor_id"], write=True)
                lease = await self.service._claim("action_dispatch", value["id"], once=True)

                def dispatching(tx):
                    current = tx.get("action", value["id"])
                    self.service._action_current(tx, current, approved=True)
                    if current["status"] != "approved" or current.get("dispatch"):
                        raise PersonalAgentError("execution_unknown", "原日历请求已进入提交阶段")
                    if current["operation"] == "update":
                        proof = current.get("preflight", {})
                        if (
                            proof.get("status") != "passed"
                            or proof.get("base_etag") != current["base_etag"]
                            or proof.get("job_id") != job.job_id
                            or proof.get("claim_epoch") != job.claim_epoch
                            or proof.get("request_sha256") != job.request_sha256
                        ):
                            raise _UpdatePreflightError(
                                "preflight_read_failed", "原对象缺少当前执行的写前核验凭据"
                            )
                    self.service._fence(tx, "action_dispatch", value["id"], lease, phase="sent")
                    return tx.put(
                        "action",
                        {
                            **current,
                            "status": "dispatching",
                            "dispatch": {
                                "job_id": job.job_id,
                                "claim_epoch": job.claim_epoch,
                                "request_sha256": job.request_sha256,
                                "started_at": self.service.stamp(),
                            },
                            "updated_at": self.service.stamp(),
                        },
                        expected_revision=current["revision"],
                    )

                value = await self.service._write(dispatching)
                await self._authorize_job(value, job)
                await self.engine.chat_run_store.append_step(
                    value["execution"]["run_id"],
                    sequence=3 if value["operation"] == "update" else 2,
                    stage="calendar_dispatch",
                    status="running",
                    summary="原行动已进入唯一提交阶段，不自动重发",
                    metadata={
                        "event_id": value["event_id"],
                        "request_sha256": job.request_sha256,
                        "model_calls": 0,
                    },
                )
                await self._authorize_job(value, job)
                await self.service.assert_token_snapshot(credential, value["actor_id"], write=True)
                try:
                    if value["operation"] == "insert":
                        await self.service.adapter.insert_event(
                            token, value["event_id"], value["body"]
                        )
                    else:
                        await self.service.adapter.update_event(
                            token, value["event_id"], value["body"], etag=value["base_etag"]
                        )
                except GoogleProtocolError as exc:
                    if exc.status == 412:
                        status, error = "conflict", exc.code
                    elif exc.status != 409:
                        raise
                if status != "conflict":
                    value = await self._save(value["id"], {"status": "submitted"})
                    await self.engine.chat_run_store.append_step(
                        value["execution"]["run_id"],
                        sequence=3 if value["operation"] == "update" else 2,
                        stage="calendar_dispatch",
                        status="completed",
                        summary="原请求已提交，等待原日历对象读回",
                        metadata={"event_id": value["event_id"], "model_calls": 0},
                    )
                    await self._authorize_job(value, job)
                    status, result = await self.service._readback_action(
                        value, actor=value["actor_id"]
                    )
                    value = await self._save(value["id"], {"status": status, "result": result})
        except _UpdatePreflightError as exc:
            status, error = "conflict", exc.code
        except GoogleProtocolError as exc:
            error = exc.code
        except PersonalAgentError as exc:
            error = exc.code
        except asyncio.CancelledError:
            error = "execution_cancelled"
        except TimeoutError:
            error = "execution_timeout"
        except Exception:
            error = "execution_error"
        finally:
            if renewal is not None:
                renewal.cancel()
                outcomes = await asyncio.gather(renewal, return_exceptions=True)
                if any(
                    isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError)
                    for item in outcomes
                ):
                    error = "execution_lease_error"
            latest = await self.service._record("action", value["id"])
            if error and error.startswith("preflight_") and latest.get("dispatch"):
                status, error = "unknown", "execution_unknown"
            if latest["status"] in {"rejected", "cancelled", "expired"} and not latest.get(
                "dispatch"
            ):
                status = latest["status"]
            elif latest["status"] == "verified":
                status = "verified"
            elif status not in {"verified", "conflict"}:
                status = "unknown" if latest.get("dispatch") else "cancelled"
            if status == "conflict" and error is None:
                error = "readback_mismatch"
            value = await self._save(
                value["id"],
                {
                    "status": status,
                    "last_error_code": error,
                    "execution_observation": {
                        "status": status,
                        "error_code": error,
                        "model_calls": 0,
                        "observed_at": self.service.stamp(),
                    },
                },
            )
            await self._settle(value, job, started=started)

    def _receipt(self, value, started_at):
        verified = value["status"] == "verified"
        completed = datetime.now(UTC)
        return CompletionReceipt.from_dict(
            {
                "schema_version": 1,
                "receipt_id": "calendar-receipt-" + value["id"],
                "run_id": value["execution"]["run_id"],
                "outcome": "completed"
                if verified
                else "partial"
                if value["status"] == "unknown"
                else "cancelled"
                if value["status"] in {"cancelled", "rejected", "expired"}
                else "failed",
                "summary": "原日历对象已读回核验"
                if verified
                else "写前检查未通过，本次没有发送修改"
                if value["operation"] == "update" and not value.get("dispatch")
                else "日历行动未获得完成证明，请核对原对象",
                "unverified": [] if verified else ["不自动重放原外部写请求"],
                "approvals": [
                    asdict(
                        ReceiptApproval(
                            call_id=value["approval"]["receipt_id"],
                            tool_name="personal_calendar_action",
                            decision="allowed_once",
                            scope=value["digest"],
                        )
                    )
                ],
                "evidence_refs": [
                    "calendar-action:" + value["id"],
                    "calendar-digest:" + value["digest"],
                ],
                "started_at": started_at,
                "completed_at": completed.isoformat(),
                "duration_ms": max(
                    0, int((completed - timestamp(started_at)).total_seconds() * 1000)
                ),
            }
        )

    async def _settle(self, value, job, *, started):
        intent = self._intent(value)
        run = await self.engine.chat_run_store.get_run(intent["session_id"], intent["run_id"])
        receipt = None
        if run is not None and run.status == "running":
            receipt = self._receipt(value, run.started_at)
            if value.get("dispatch"):
                await self.engine.chat_run_store.append_step(
                    intent["run_id"],
                    sequence=3 if value["operation"] == "update" else 2,
                    stage="calendar_dispatch",
                    status="completed" if value["status"] == "verified" else "failed",
                    summary="原外部请求已通过对象读回核验"
                    if value["status"] == "verified"
                    else "唯一提交阶段已结束，原请求结果按对账事实保留",
                    metadata={
                        "event_id": value["event_id"],
                        "action_status": value["status"],
                        "error_code": value.get("last_error_code"),
                        "model_calls": 0,
                    },
                )
            await self.engine.chat_run_store.append_step(
                intent["run_id"],
                sequence=4 if value["operation"] == "update" else 3,
                stage="calendar_readback",
                status="completed" if value["status"] == "verified" else "failed",
                summary=receipt.summary,
                metadata={
                    "action_status": value["status"],
                    "model_calls": 0,
                    "usage_kind": "deterministic_protocol_no_model",
                    "result": value.get("result"),
                },
            )
            await self.engine.chat_run_store.finish_run(
                intent["run_id"],
                status=_run_status(value["status"]),
                receipt=receipt,
                usage=None,
            )
        elif run is not None:
            receipt = run.receipt
        current = await self.jobs.get(job.job_id)
        if current is None:
            raise PersonalAgentError("storage_error", "原日历 Job 缺失，未重新受理")
        if (
            current.state is AgentJobState.RUNNING
            and timestamp(current.claim_expires_at) <= self.service.now()
        ):
            await self.jobs.mark_recovery_unknown(
                job.job_id,
                expected_request_sha256=job.request_sha256,
                expected_session_id_sha256=job.request.session_id_sha256,
                expected_claim_epoch=job.claim_epoch,
                expected_latest_receipt_sha256=current.latest_receipt.receipt_sha256,
            )
            return
        if not started and current.state is AgentJobState.CLAIMED:
            await self.jobs.cancel_claimed_before_start(
                job.job_id, owner_id=self.owner, claim_epoch=job.claim_epoch
            )
        elif current.state is AgentJobState.RUNNING and current.claim_owner_id == self.owner:
            if value["status"] == "unknown" or receipt is None:
                await self.jobs.mark_execution_unknown(
                    job.job_id,
                    owner_id=self.owner,
                    claim_epoch=job.claim_epoch,
                    expected_request_sha256=job.request_sha256,
                    expected_session_id_sha256=job.request.session_id_sha256,
                    expected_latest_receipt_sha256=current.latest_receipt.receipt_sha256,
                )
            else:
                response = canonical(
                    {
                        "type": "personal_calendar_action_result",
                        "version": 1,
                        "action_id": value["id"],
                        "digest": value["digest"],
                        "status": value["status"],
                        "run_id": intent["run_id"],
                        "receipt_sha256": digest(json.loads(json.dumps(receipt.to_dict()))),
                        "model_calls": 0,
                        "usage_kind": "deterministic_protocol_no_model",
                        "result": value.get("result"),
                    }
                )
                outcome = (
                    "completed"
                    if value["status"] == "verified"
                    else "cancelled"
                    if value["status"] in {"cancelled", "rejected", "expired"}
                    else "error"
                )
                result = issue_agent_worker_result(
                    request=job.request,
                    status=outcome,
                    response=response,
                    error=value.get("last_error_code") or "",
                    total_tokens=0,
                    total_cost_usd=0,
                    turns=0,
                    completed_at=self.service.stamp(),
                )
                await self.jobs.finish(
                    job.job_id,
                    owner_id=self.owner,
                    claim_epoch=job.claim_epoch,
                    result=result,
                    terminal_payload=AgentJobTerminalPayload(
                        response=response, error=value.get("last_error_code") or ""
                    ),
                )
                await self._deliver(value, await self.jobs.get(job.job_id))

    async def _deliver(self, value, job):
        publication = await self.jobs.get_job_publication(job.job_id)
        if publication is None:
            raise PersonalAgentError("storage_error", "日历结果缺少原 publication")
        if publication.state is AgentJobPublicationState.PUBLISHED:
            records = await self.jobs.list_result_inbox_records(value["execution"]["session_id"])
            matching = [
                record.delivery
                for record in records
                if record.delivery.publication_id == publication.publication_id
            ]
            if len(matching) != 1:
                raise PersonalAgentError("storage_error", "日历 publication 缺少原投递回执")
            delivery = matching[0]
            if value.get("delivery_id") != delivery.delivery_id:
                await self._save(
                    value["id"],
                    {
                        "publication_id": publication.publication_id,
                        "delivery_id": delivery.delivery_id,
                        "delivery_sha256": delivery.delivery_sha256,
                    },
                )
            return
        try:
            claimed = await self.jobs.claim_publication(
                publication.publication_id, owner_id=self.owner, lease_seconds=90
            )
        except AgentJobLifecycleConflictError:
            return
        try:
            content = await self.jobs.recover_publication_content(
                publication.publication_id,
                owner_id=self.owner,
                claim_epoch=claimed.publication.claim_epoch,
            )
            if content.request != action_request(
                value["execution"]
            ) or content.payload != AgentJobPayload(**value["execution"]["payload"]):
                raise PersonalAgentError("binding_conflict", "日历结果 publication 与原行动不一致")
            delivery = await self.jobs.deliver_publication_to_inbox(
                publication.publication_id,
                owner_id=self.owner,
                claim_epoch=claimed.publication.claim_epoch,
            )
            await self._save(
                value["id"],
                {
                    "publication_id": publication.publication_id,
                    "delivery_id": delivery.delivery.delivery_id,
                    "delivery_sha256": delivery.delivery.delivery_sha256,
                },
            )
        except BaseException:
            await self.jobs.release_publication_claim(
                publication.publication_id,
                owner_id=self.owner,
                claim_epoch=claimed.publication.claim_epoch,
            )
            raise

    async def _recover_interrupted(self, value, job):
        if job is not None and job.state is AgentJobState.RUNNING:
            if timestamp(job.claim_expires_at) > self.service.now():
                return
            await self.jobs.mark_recovery_unknown(
                job.job_id,
                expected_request_sha256=job.request_sha256,
                expected_session_id_sha256=job.request.session_id_sha256,
                expected_claim_epoch=job.claim_epoch,
                expected_latest_receipt_sha256=job.latest_receipt.receipt_sha256,
            )
        elif job is not None and job.state not in TERMINAL_AGENT_JOB_STATES:
            return
        observed_status = (
            value["status"] if value["status"] in {"verified", "conflict"} else "unknown"
        )
        value = await self._save(
            value["id"],
            {
                "status": observed_status,
                "last_error_code": "interrupted_dispatch",
                "execution_observation": {
                    "status": "unknown",
                    "error_code": "interrupted_dispatch",
                    "observed_at": self.service.stamp(),
                    "model_calls": 0,
                },
            },
        )
        intent = self._intent(value)
        run = await self.engine.chat_run_store.get_run(intent["session_id"], intent["run_id"])
        if run is not None and run.status == "running":
            await self.engine.chat_run_store.finish_run(
                run.id,
                status=_run_status(value["status"]),
                receipt=self._receipt(value, run.started_at),
                usage=None,
            )

    async def stop(self):
        async with self._lock:
            self._stopping = True
            tasks = list(self.active.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._observe_finished()
