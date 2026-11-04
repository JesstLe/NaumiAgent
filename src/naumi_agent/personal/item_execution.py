"""Single-Job personal-item execution, evidence publication and human-read projection."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

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
from naumi_agent.model.router import ModelTier
from naumi_agent.personal.artifact_models import normalize_refs, strict_json_loads
from naumi_agent.personal.engine_execution import (
    bounded_execution,
    make_execution_child,
    renew_execution_lease,
)
from naumi_agent.personal.item_models import canonical, digest, normalize_intent
from naumi_agent.personal.models import PersonalAgentError

logger = logging.getLogger(__name__)


def main_policy_sha256(engine):
    """Bind routing and effective limits, never credential bytes or mutable UI state."""
    safety = engine.config.safety
    return digest(
        {
            "workspace_key": engine.personal_agents.workspace_key,
            "session_db_key": engine.personal_agents.session_db_key,
            "tools": sorted(engine.tool_registry.names),
            "models": {tier.value: engine.router.resolve_model(tier) for tier in ModelTier},
            "permission_mode": str(safety.permission_mode),
            "max_turns": safety.max_turns,
            "max_budget_usd": safety.max_budget_usd,
            "max_input_tokens": safety.max_input_tokens,
            "max_output_tokens": safety.max_output_tokens,
        }
    )


def _request(intent):
    fields = dict(intent["request"])
    fields["tool_scope"] = tuple(fields["tool_scope"])
    return AgentWorkerRequest(**fields)


class PersonalItemExecutionContext:
    """Runtime-only authority used by the root's real ArtifactToolProxy hook."""

    force_private = True

    def __init__(self, service, intent, job, child):
        self.service, self.intent, self.job, self.child = service, intent, job, child
        self.policy = intent["policy"]
        self.input_refs = intent["input_refs"]
        self.input_paths = {}
        self.model_calls = 0
        self.usage_calls = 0
        self.process_workspace = Path(child._execution_data_dir) / "empty-input-workspace"
        self.process_workspace.mkdir(parents=True, exist_ok=True)
        if self.process_workspace.is_symlink() or any(self.process_workspace.iterdir()):
            raise PersonalAgentError("binding_conflict", "事项隔离输入目录不为空，已停止执行")

    async def initialize(self):
        for ref in self.input_refs:
            value = await self.child.personal_artifacts.get(
                actor_id=self.intent["actor_id"],
                artifact_id=ref["artifact_id"],
                version=ref["version"],
            )
            path = f"/inputs/{ref['artifact_id']}-v{ref['version']}{value['suffix']}"
            self.input_paths[path] = ref

    def observe_model_usage(self):
        original_stream, original_call = self.child.router.stream, self.child.router.call

        async def stream(*args, **kwargs):
            self.model_calls += 1
            known = False
            try:
                async for chunk in original_stream(*args, **kwargs):
                    known = known or getattr(chunk, "usage", None) is not None
                    yield chunk
            finally:
                if known:
                    self.usage_calls += 1

        async def call(*args, **kwargs):
            self.model_calls += 1
            response = await original_call(*args, **kwargs)
            if getattr(response, "usage", None) is not None:
                self.usage_calls += 1
            return response

        self.child.router.stream, self.child.router.call = stream, call

    async def authorize(self):
        await self.service._validate(self.intent)
        current = await self.service.jobs.get(self.job.job_id)
        if (
            current is None
            or current.state is not AgentJobState.RUNNING
            or current.request != self.job.request
            or current.claim_owner_id != self.service._owner
            or current.claim_epoch != self.job.claim_epoch
            or not current.claim_expires_at
            or datetime.fromisoformat(current.claim_expires_at) <= datetime.now(UTC)
        ):
            raise PersonalAgentError("execution_fenced", "事项执行租约已失效，已停止后续操作")
        session = self.child._session
        if (
            session is None
            or session.id != self.intent["execution_session_id"]
            or session.personal_execution_id != self.intent["task_id"]
            or session.personal_agent_id
            != ("" if self.intent["actor_id"] == "main" else self.intent["actor_id"])
        ):
            raise PersonalAgentError("binding_conflict", "事项执行身份与原始委托不一致")

    async def authorize_tool(self, name, args):
        await self.authorize()
        if not isinstance(args, dict) or name not in self.policy["tool_names"]:
            raise PersonalAgentError("scope_denied", "该工具不属于本次事项授权")
        if name in {"code_execute", "bash_run"}:
            return
        if name in {"file_read", "read"}:
            path = args.get("path", args.get("file_path"))
            if path not in self.input_paths:
                raise PersonalAgentError("scope_denied", "本次事项仅读取确切的 /inputs 资料路径")
            return
        if name != "personal_artifacts":
            raise PersonalAgentError("scope_denied", "本次事项未授权该操作")
        operation = args.get("operation")
        if operation not in self.policy["artifact_operations"]:
            raise PersonalAgentError("scope_denied", "本次事项未授权该资料操作")
        refs = [*self.input_refs, *getattr(self.child, "_personal_artifact_outputs", [])]
        if operation in {"read", "get"}:
            if not any(
                ref["artifact_id"] == args.get("artifact_id")
                and ref["version"] == args.get("version")
                for ref in refs
            ):
                raise PersonalAgentError("scope_denied", "该确切资料版本未包含在本次事项中")
        elif operation == "publish":
            if (
                set(args)
                - {"operation", "request_id", "title", "filename", "content", "kind", "source_refs"}
                or args.get("kind", "output") != "output"
            ):
                raise PersonalAgentError(
                    "scope_denied", "本次事项只保存自身报告，不导入或修改其他资料"
                )
            allowed = {canonical(ref) for ref in refs}
            if any(
                canonical(ref) not in allowed for ref in normalize_refs(args.get("source_refs", []))
            ):
                raise PersonalAgentError("scope_denied", "报告引用了未授权的来源")
        else:
            raise PersonalAgentError("scope_denied", "本次事项未授权该资料操作")

    async def read_input(self, path):
        await self.authorize()
        ref = self.input_paths.get(path)
        if ref is None:
            raise PersonalAgentError("scope_denied", "请选择本次确切的输入文件")
        _, content = await self.child.personal_artifacts.read(
            actor_id=self.intent["actor_id"],
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
        return content


class _RunBindingSink:
    def __init__(self, service, intent):
        self.service, self.intent, self.run_id = service, intent, ""

    async def emit(self, event):
        if event.session_id != self.intent["execution_session_id"] or not event.run_id:
            raise PersonalAgentError("binding_conflict", "事项运行事件缺少确切身份")
        if self.run_id and event.run_id != self.run_id:
            raise PersonalAgentError("binding_conflict", "一次事项出现不同运行编号")
        if not self.run_id:
            self.service._bind(self.intent, run_id=event.run_id)
            self.run_id = event.run_id


class PersonalItemExecutionService:
    def __init__(self, engine, store, owner_key, *, validate_intent):
        self.engine, self.store, self.owner_key = engine, store, owner_key
        self.validate_intent = validate_intent
        self.jobs = engine.personal_collaborations.jobs
        self.namespace = "personal-engine-v1:" + engine.personal_agents.workspace_key
        self._owner = "personal-items-" + uuid.uuid4().hex
        self._active = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._child_factory = make_execution_child

    def prepare(
        self,
        item,
        *,
        occurrence_id,
        execution_session_id,
        task,
        context,
        input_refs,
        input_manifest,
        schedule_id,
        event_id,
    ):
        definition = item["definition"]
        actor, policy = definition["assignee"]["id"], definition["policy"]
        issued_at = item["created_at"]
        envelope = {
            "type": "personal_item_context",
            "version": 1,
            "occurrence_id": occurrence_id,
            "item_id": item["item_id"],
            "definition_sha256": item["definition_sha256"],
            "once_key": item["once_key"],
            "policy": policy,
            "input_refs": input_refs,
            "input_manifest": input_manifest,
            "materials": context,
        }
        payload = AgentJobPayload(
            task_id=occurrence_id,
            session_id=execution_session_id,
            task=task,
            context=canonical(envelope),
            message_topic="personal-item:" + occurrence_id,
        )
        request = issue_agent_worker_request(
            **asdict(payload),
            agent_name=actor,
            tool_scope=policy["tool_names"],
            permission_mode=self.engine.config.safety.permission_mode,
            model_tier=policy["model_tier"],
            max_turns=policy["max_turns"],
            max_budget_usd=(
                None
                if policy["max_cost_microusd"] is None
                else policy["max_cost_microusd"] / 1_000_000
            ),
            timeout_seconds=policy["timeout_seconds"],
            issued_at=issued_at,
        )
        intent = {
            "occurrence_id": occurrence_id,
            "item_id": item["item_id"],
            "item_revision": item["revision"],
            "once_key": item["once_key"],
            "definition_sha256": item["definition_sha256"],
            "workspace_key": definition["route"]["workspace_key"],
            "schedule_id": schedule_id,
            "event_id": event_id,
            "execution_session_id": execution_session_id,
            "task_id": occurrence_id,
            "actor_id": actor,
            "agent_version": definition["assignee"]["version"],
            "input_refs": input_refs,
            "input_manifest": input_manifest,
            "policy": policy,
            "main_policy_sha256": main_policy_sha256(self.engine) if actor == "main" else "",
            "request": {**asdict(request), "tool_scope": list(request.tool_scope)},
            "payload": asdict(payload),
            "issued_at": request.issued_at,
        }
        intent["prepared_sha256"] = digest(intent)
        return normalize_intent(intent)

    def _load(self, intent):
        normalized = normalize_intent(intent)
        saved = self.store.get_intent(
            owner_key=self.owner_key, occurrence_id=normalized["occurrence_id"]
        )
        if normalize_intent(saved) != normalized:
            raise PersonalAgentError("binding_conflict", "事项执行意图与持久原件不一致")
        if (
            normalized["workspace_key"] != self.engine.personal_agents.workspace_key
            or _request(normalized).agent_name != normalized["actor_id"]
        ):
            raise PersonalAgentError("binding_conflict", "事项请求不属于当前执行上下文")
        return saved

    def _bind(self, intent, **references):
        return self.store.bind(
            owner_key=self.owner_key,
            occurrence_id=intent["occurrence_id"],
            expected_prepared_sha256=intent["prepared_sha256"],
            references=references,
        )

    async def _validate(self, intent):
        await self.validate_intent(intent)
        request = _request(intent)
        if request.permission_mode != str(self.engine.config.safety.permission_mode):
            raise PersonalAgentError("version_conflict", "事项权限策略已变化")
        if intent["actor_id"] == "main":
            if intent["main_policy_sha256"] != main_policy_sha256(self.engine):
                raise PersonalAgentError("version_conflict", "主 Agent 执行策略已变化")
        else:
            profile = self.engine.personal_agents._executable(intent["actor_id"])
            if profile.version != intent["agent_version"]:
                raise PersonalAgentError("version_conflict", "Agent 配置已变化")
            if not set(request.tool_scope) <= set(profile.tool_names):
                raise PersonalAgentError("scope_denied", "Agent 工具授权已变化")
        await self.engine.personal_artifacts.authorize_refs(
            actor_id=intent["actor_id"],
            refs=intent["input_refs"],
        )

    async def start(self):
        self._closing = False

    async def stop(self):
        self._closing = True
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def submit(self, intent):
        return await self.reconcile(intent)

    async def reconcile(self, intent):
        async with self._lock:
            intent = self._load(intent)
            request = _request(intent)
            job_id = agent_job_id_for_request(request)
            job = await self.jobs.get(job_id)
            if job is None and intent.get("references", {}).get("job_id"):
                raise PersonalAgentError("storage_error", "已受理事项的 Job 原件缺失，未重新执行")
            if job is not None and job.request != request:
                raise PersonalAgentError("binding_conflict", "事项 Job 与原请求不一致")
            if job is not None and job.state in TERMINAL_AGENT_JOB_STATES:
                await self._deliver(intent, job)
                return await self.view(intent)
            if job is not None and job.state is AgentJobState.RUNNING:
                if datetime.fromisoformat(job.claim_expires_at) <= datetime.now(UTC):
                    try:
                        await self.jobs.mark_recovery_unknown(
                            job_id,
                            expected_request_sha256=request.request_sha256,
                            expected_session_id_sha256=request.session_id_sha256,
                            expected_claim_epoch=job.claim_epoch,
                            expected_latest_receipt_sha256=job.latest_receipt.receipt_sha256,
                        )
                    except AgentJobLifecycleConflictError:
                        pass
                return await self.view(intent)
            item = self.store.get(owner_key=self.owner_key, item_id=intent["item_id"])
            if item["state"] in {"cancelled", "closed", "paused"}:
                if job is not None and job.state is AgentJobState.ADMITTED:
                    await self.jobs.cancel_before_claim(job_id)
                return await self.view(intent)
            if self._closing or job_id in self._active:
                return await self.view(intent)
            try:
                await self._validate(intent)
            except PersonalAgentError:
                if job is not None and job.state is AgentJobState.ADMITTED:
                    await self.jobs.cancel_before_claim(job_id)
                raise
            transition = None
            try:
                if job is None:
                    transition = await self.jobs.admit_for_capacity(
                        request=request,
                        payload=AgentJobPayload(**intent["payload"]),
                        owner_id=self._owner,
                        lease_seconds=90,
                        max_active_jobs=self.engine.config.safety.max_parallel_agents,
                        max_waiters=self.engine.config.safety.max_queued_agents,
                    )
                    job = transition.job
                self._bind(intent, job_id=job.job_id, namespace=self.namespace)
                if transition is None:
                    transition = await self.jobs.claim_for_capacity(
                        job_id,
                        owner_id=self._owner,
                        lease_seconds=90,
                        max_active_jobs=self.engine.config.safety.max_parallel_agents,
                        max_waiters=self.engine.config.safety.max_queued_agents,
                    )
            except (AgentJobCapacityExhaustedError, AgentJobLifecycleConflictError):
                return await self.view(intent)
            if transition.should_dispatch:
                task = asyncio.create_task(
                    self._execute(intent, transition.job),
                    name="personal-item:" + intent["occurrence_id"],
                )
                self._active[job_id] = task
                task.add_done_callback(lambda done, key=job_id: self._done(key, done))
            return await self.view(intent)

    def _done(self, key, task):
        if self._active.get(key) is task:
            self._active.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "Personal item lost terminal evidence: %s", type(task.exception()).__name__
            )

    async def cancel(self, intent):
        intent = self._load(intent)
        item = self.store.get(owner_key=self.owner_key, item_id=intent["item_id"])
        same_once = (
            item["once_key"] == intent["once_key"]
            and item["definition_sha256"] == intent["definition_sha256"]
        )
        if same_once and item["state"] not in {"cancelled", "closed"}:
            self.store.control(
                owner_key=self.owner_key,
                item_id=item["item_id"],
                expected_revision=item["revision"],
                request_id="execution-cancel-" + intent["occurrence_id"],
                action="cancel",
            )
        job_id = agent_job_id_for_request(_request(intent))
        active = self._active.get(job_id)
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if not same_once:
            job = await self.jobs.get(job_id)
            if job is not None and job.state is AgentJobState.ADMITTED:
                await self.jobs.cancel_before_claim(job_id)
            return await self.view(intent)
        return await self.reconcile(intent)

    async def _execute(self, intent, job):
        child = renewal = None
        started = False
        uncertain = False
        sink = _RunBindingSink(self, intent)
        status, error, manifest, usage = "error", "", None, None
        try:
            await self._validate(intent)
            payload = await self.jobs.recover_payload(
                job.job_id,
                owner_id=self._owner,
                claim_epoch=job.claim_epoch,
            )
            if payload != AgentJobPayload(**intent["payload"]):
                raise PersonalAgentError("binding_conflict", "事项执行正文与原件不一致")
            marker = "" if intent["actor_id"] == "main" else intent["actor_id"]
            if marker:
                self.engine.personal_agents.store.register_execution_session(
                    marker,
                    intent["workspace_key"],
                    intent["execution_session_id"],
                    session_db_key=self.engine.personal_agents.session_db_key,
                )
            existing = await self.engine.session_store.load(intent["execution_session_id"])
            if existing is not None and (
                existing.messages
                or existing.personal_agent_id != marker
                or existing.personal_execution_id != intent["task_id"]
            ):
                raise PersonalAgentError("binding_conflict", "事项执行会话已有其他内容，未覆盖")
            if existing is None:
                await self.engine.session_store.save(
                    Session(
                        id=intent["execution_session_id"],
                        title="事项执行",
                        personal_agent_id=marker,
                        personal_execution_id=intent["task_id"],
                        workspace_root=str(self.engine.workspace_root),
                        model=self.engine.router.resolve_model(ModelTier(job.request.model_tier)),
                    )
                )
            child = self._child_factory(self.engine, job)
            if not await child.load_session(intent["execution_session_id"]):
                raise PersonalAgentError("not_found", "事项执行会话缺失")
            context = PersonalItemExecutionContext(self, intent, job, child)
            child._personal_collaboration_task_id = intent["task_id"]
            child._personal_execution_authorizer = context.authorize
            child._personal_item_context = context
            child._personal_execution_model_tier = job.request.model_tier
            child._personal_effective_tools = frozenset(job.request.tool_scope)
            context.observe_model_usage()
            await context.initialize()
            await child.personal_artifacts.bind_refs(
                intent["execution_session_id"],
                intent["actor_id"],
                intent["input_refs"],
            )
            await self._validate(intent)
            await self.jobs.mark_running(
                job.job_id, owner_id=self._owner, claim_epoch=job.claim_epoch
            )
            started = True
            renewal = asyncio.create_task(
                renew_execution_lease(
                    self.jobs,
                    job,
                    self._owner,
                    asyncio.current_task(),
                    context.authorize,
                )
            )
            result = await bounded_execution(
                child.run_streaming(payload.task, sink, turn_context=payload.context),
                timeout_seconds=job.request.timeout_milliseconds / 1000,
            )
            await context.authorize()
            run = await child.chat_run_store.get_run(intent["execution_session_id"], sink.run_id)
            receipt = getattr(result, "receipt", None)
            if (
                run is None
                or receipt is None
                or run.receipt is None
                or receipt.run_id != sink.run_id
                or run.receipt != receipt
            ):
                raise PersonalAgentError("binding_conflict", "事项缺少确切的持久运行回执")
            if run.usage is None or context.model_calls != context.usage_calls:
                raise PersonalAgentError("usage_unknown", "运行用量缺失，预算保持未知，未自动重试")
            usage = run.usage.model_dump(mode="json")
            status = (
                result.status if result.status in {"completed", "max_turns", "error"} else "error"
            )
            if status == "completed" and (
                receipt.outcome != "completed" or not result.response.strip()
            ):
                raise PersonalAgentError("output_unverified", "运行回执未证明事项完成")
            artifacts = []
            if status == "completed":
                artifacts = await self._report(child, context, intent, job, run, result.response)
            manifest = {
                "version": 1,
                "type": "personal_item_result",
                "occurrence_id": intent["occurrence_id"],
                "item_id": intent["item_id"],
                "run_id": sink.run_id,
                "receipt_id": receipt.receipt_id,
                "receipt_sha256": digest(receipt.to_dict()),
                "answer": result.response,
                "artifacts": artifacts,
                "usage_known": usage is not None,
                "usage": usage,
            }
            error = result.error or ""
        except asyncio.CancelledError:
            status, error = "cancelled", "事项执行已停止；不会自动重复执行"
        except TimeoutError:
            status, error = "timeout", "事项执行超过时限；不会自动重复执行"
        except PersonalAgentError as exc:
            status, error = "error", exc.message
            uncertain = exc.code in {"usage_unknown", "execution_unknown"}
            if exc.code in {"occurrence_paused", "cancelled", "occurrence_cancelled"}:
                status = "cancelled"
        except Exception as exc:
            logger.warning("Personal item execution failed: %s", type(exc).__name__)
            status = "error"
            error = "事项执行未完成，请查看真实运行记录"
        finally:
            try:
                if renewal is not None:
                    renewal.cancel()
                    await asyncio.gather(renewal, return_exceptions=True)
                current = await self.jobs.get(job.job_id)
                if current is not None and current.claim_owner_id == self._owner:
                    if not started and current.state is AgentJobState.CLAIMED:
                        await self.jobs.cancel_claimed_before_start(
                            job.job_id,
                            owner_id=self._owner,
                            claim_epoch=job.claim_epoch,
                        )
                    elif started and current.state is AgentJobState.RUNNING:
                        observed = None
                        if sink.run_id and child is not None:
                            observed = await child.chat_run_store.get_run(
                                intent["execution_session_id"],
                                sink.run_id,
                            )
                        if observed is None or observed.receipt is None:
                            uncertain = True
                        if usage is None and sink.run_id and child is not None:
                            observed = await child.chat_run_store.get_run(
                                intent["execution_session_id"],
                                sink.run_id,
                            )
                            if (
                                observed
                                and observed.usage is not None
                                and (context.model_calls == context.usage_calls)
                            ):
                                usage = observed.usage.model_dump(mode="json")
                        if uncertain or usage is None:
                            await self.jobs.mark_execution_unknown(
                                job.job_id,
                                owner_id=self._owner,
                                claim_epoch=job.claim_epoch,
                                expected_request_sha256=job.request_sha256,
                                expected_session_id_sha256=job.request.session_id_sha256,
                                expected_latest_receipt_sha256=current.latest_receipt.receipt_sha256,
                            )
                            return
                        if manifest is None:
                            manifest = {
                                "version": 1,
                                "type": "personal_item_result",
                                "occurrence_id": intent["occurrence_id"],
                                "item_id": intent["item_id"],
                                "run_id": sink.run_id,
                                "receipt_id": observed.receipt.receipt_id,
                                "receipt_sha256": digest(observed.receipt.to_dict()),
                                "answer": "",
                                "artifacts": [],
                                "usage_known": True,
                                "usage": usage,
                            }
                        if status == "completed":
                            try:
                                await context.authorize()
                            except PersonalAgentError as exc:
                                status, error = "error", exc.message
                                manifest.update(answer="", artifacts=[])
                        response = canonical(manifest)
                        result_receipt = issue_agent_worker_result(
                            request=job.request,
                            status=status,
                            response=response,
                            error=error,
                            total_tokens=(usage["input_tokens"] + usage["output_tokens"])
                            if usage
                            else 0,
                            total_cost_usd=float(usage["reported_cost_usd"]) if usage else 0,
                            turns=usage["turns"] if usage else 0,
                            completed_at=datetime.now(UTC).isoformat(),
                        )
                        await self.jobs.finish(
                            job.job_id,
                            owner_id=self._owner,
                            claim_epoch=job.claim_epoch,
                            result=result_receipt,
                            terminal_payload=AgentJobTerminalPayload(
                                response=response, error=error
                            ),
                        )
                        await self._deliver(intent, await self.jobs.get(job.job_id))
            finally:
                if child is not None:
                    await child.shutdown()

    async def _report(self, child, context, intent, job, run, answer):
        await context.authorize()
        outputs = normalize_refs(getattr(child, "_personal_artifact_outputs", []))
        has_report = False
        for ref in outputs:
            await context.authorize()
            snapshot = await child.personal_artifacts.get(
                actor_id=intent["actor_id"], artifact_id=ref["artifact_id"], version=ref["version"]
            )
            has_report = has_report or snapshot["suffix"] == ".md"
            if snapshot["suffix"] == ".json":
                _, content = await child.personal_artifacts.read(
                    actor_id=intent["actor_id"],
                    artifact_id=ref["artifact_id"],
                    version=ref["version"],
                )
                document = strict_json_loads(content)
                has_report = (
                    has_report or isinstance(document, dict) and document.get("type") == "page"
                )
        if not has_report:
            await context.authorize()
            value = await child.personal_artifacts.publish(
                actor_id=intent["actor_id"],
                request_id="item-report-" + job.job_id,
                title="事项报告",
                kind="output",
                filename="report.md",
                content=answer.encode("utf-8"),
                source_refs=intent["input_refs"],
                session_id=intent["execution_session_id"],
                _trusted_execution=True,
                evidence={
                    "source_kind": "personal_item_report",
                    "run_id": run.id,
                    "session_id": intent["execution_session_id"],
                    "job_id": job.job_id,
                    "receipt_id": run.receipt.receipt_id,
                },
            )
            outputs.append(child.personal_artifacts.reference(value))
        session = await child.session_store.load(intent["execution_session_id"])
        finals = [
            m
            for m in session.messages
            if m.get("role") == "assistant"
            and not m.get("tool_calls")
            and m.get("content") == answer
        ]
        if not finals:
            raise PersonalAgentError("output_unverified", "报告缺少本轮真实最终消息")
        final = finals[-1]
        final["metadata"] = {
            **final.get("metadata", {}),
            "artifact_refs": outputs,
            "run_id": run.id,
        }
        await child.session_store.save(session)
        for ref in outputs:
            await child.chat_run_store.append_artifact(
                run.id,
                artifact_id="item-report-" + ref["artifact_id"],
                kind="personal_item_report",
                title="事项报告",
                summary=ref,
                status="ready",
                metadata={"artifact_ref": ref},
            )
        return outputs

    async def _deliver(self, intent, job):
        if job is None or job.result is None:
            return
        publication = await self.jobs.get_job_publication(job.job_id)
        if publication is None:
            raise PersonalAgentError("storage_error", "事项结果缺少持久 publication")
        self._bind(intent, publication_id=publication.publication_id)
        if publication.state is AgentJobPublicationState.PUBLISHED:
            for record in await self.jobs.list_result_inbox_records(intent["execution_session_id"]):
                delivery = record.delivery
                if delivery.publication_id == publication.publication_id:
                    self._bind(
                        intent,
                        delivery_id=delivery.delivery_id,
                        delivery_sha256=delivery.delivery_sha256,
                    )
                    return
            raise PersonalAgentError("storage_error", "已发布事项结果缺少原始投递回执")
        claimed = None
        try:
            claimed = await self.jobs.claim_publication(
                publication.publication_id, owner_id=self._owner, lease_seconds=90
            )
            content = await self.jobs.recover_publication_content(
                publication.publication_id,
                owner_id=self._owner,
                claim_epoch=claimed.publication.claim_epoch,
            )
            if content.request != _request(intent) or content.payload != AgentJobPayload(
                **intent["payload"]
            ):
                raise PersonalAgentError("binding_conflict", "事项 publication 与原请求不一致")
            delivery = await self.jobs.deliver_publication_to_inbox(
                publication.publication_id,
                owner_id=self._owner,
                claim_epoch=claimed.publication.claim_epoch,
            )
            self._bind(
                intent,
                delivery_id=delivery.delivery.delivery_id,
                delivery_sha256=delivery.delivery.delivery_sha256,
            )
        except AgentJobLifecycleConflictError:
            return
        except BaseException:
            if claimed is not None:
                await self.jobs.release_publication_claim(
                    publication.publication_id,
                    owner_id=self._owner,
                    claim_epoch=claimed.publication.claim_epoch,
                )
            raise

    async def view(self, intent):
        intent = self._load(intent)
        request = _request(intent)
        job_id = agent_job_id_for_request(request)
        job = await self.jobs.get(job_id)
        references = intent.get("references", {})
        value = {
            "occurrence_id": intent["occurrence_id"],
            "item_id": intent["item_id"],
            "execution_session_id": intent["execution_session_id"],
            "status": "prepared",
            "job_id": job_id if job else "",
            "run_id": references.get("run_id", ""),
            "answer": "",
            "artifacts": [],
            "error": "",
            "receipt": None,
            "delivery_id": "",
            "delivery_sha256": "",
            "read_receipt": None,
            "usage_known": False,
            "usage": None,
            "namespace": self.namespace,
        }
        if job is None:
            if references.get("job_id"):
                raise PersonalAgentError("storage_error", "已受理事项的 Job 原件缺失")
            item = self.store.get(owner_key=self.owner_key, item_id=intent["item_id"])
            if item["state"] in {"cancelled", "closed"} or item["once_key"] != intent["once_key"]:
                value.update(
                    status="cancelled",
                    usage_known=True,
                    usage={"input_tokens": 0, "output_tokens": 0, "reported_cost_usd": "0"},
                )
            return value
        if job.request != request:
            raise PersonalAgentError("binding_conflict", "事项 Job 原件与请求不一致")
        value["status"] = {"admitted": "queued", "claimed": "running"}.get(
            job.state.value, job.state.value
        )
        value["job_state"] = job.state.value
        if job.state in {AgentJobState.CANCELLED, AgentJobState.UNKNOWN} and job.result is None:
            value["usage_known"] = job.state is AgentJobState.CANCELLED
            value["usage"] = (
                {"input_tokens": 0, "output_tokens": 0, "reported_cost_usd": "0"}
                if value["usage_known"]
                else None
            )
        try:
            await self.engine.personal_artifacts.authorize_refs(
                actor_id=intent["actor_id"],
                refs=intent["input_refs"],
            )
        except PersonalAgentError:
            value.update(
                answer="",
                artifacts=[],
                receipt=None,
                steps=[],
                error="事项资料授权已失效，正文暂不可查看",
                output_available=False,
            )
            return await self._inbox_view(intent, job_id, value)
        if value["run_id"]:
            run = await self.engine.chat_run_store.get_run(
                intent["execution_session_id"], value["run_id"]
            )
            if run is not None:
                value["receipt"] = run.receipt.to_dict() if run.receipt else None
                value["steps"] = [asdict(s) for s in run.steps]
        if job.result is not None:
            payload = await self.jobs.recover_terminal_payload(
                job_id,
                expected_result_sha256=job.result.result_sha256,
            )
            result = strict_json_loads(payload.response)
            if (
                result.get("type") != "personal_item_result"
                or result.get("version") != 1
                or result.get("occurrence_id") != intent["occurrence_id"]
                or result.get("item_id") != intent["item_id"]
                or result.get("run_id", "") != value["run_id"]
            ):
                raise PersonalAgentError("binding_conflict", "事项结果 manifest 与原运行不一致")
            actual = await self.engine.chat_run_store.get_run(
                intent["execution_session_id"],
                result["run_id"],
            )
            if (
                actual is None
                or actual.receipt is None
                or actual.receipt.receipt_id != result.get("receipt_id")
                or digest(actual.receipt.to_dict()) != result.get("receipt_sha256")
            ):
                raise PersonalAgentError("storage_error", "事项原始运行或回执缺失，未确认成功")
            if job.state is AgentJobState.COMPLETED and (
                actual.receipt.outcome != "completed" or not result["artifacts"]
            ):
                raise PersonalAgentError("binding_conflict", "事项结果与完成回执不一致")
            value.update(
                answer=result["answer"],
                artifacts=result["artifacts"],
                error=payload.error,
                usage_known=result.get("usage_known", False),
                usage=result.get("usage"),
            )
            try:
                for ref in value["artifacts"]:
                    await self.engine.personal_artifacts.get(
                        actor_id=intent["actor_id"],
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
            except PersonalAgentError:
                value.update(
                    answer="",
                    artifacts=[],
                    output_available=False,
                    receipt=None,
                    steps=[],
                    error="事项报告授权已失效，正文暂不可查看",
                )
            else:
                value["output_available"] = bool(value["artifacts"])
        return await self._inbox_view(intent, job_id, value)

    async def _inbox_view(self, intent, job_id, value):
        inbox = await self.jobs.list_result_inbox_records(intent["execution_session_id"])
        matching = [r for r in inbox if r.delivery.job_id == job_id]
        if len(matching) > 1:
            raise PersonalAgentError("binding_conflict", "事项出现多个不同结果投递")
        if matching:
            delivery = matching[0].delivery
            value.update(
                delivery_id=delivery.delivery_id,
                delivery_sha256=delivery.delivery_sha256,
                publication_id=delivery.publication_id,
            )
            value["read_receipt"] = await self.jobs.get_human_result_inbox_read(
                intent["execution_session_id"],
                delivery.delivery_id,
                expected_delivery_sha256=delivery.delivery_sha256,
            )
        return value

    async def ack(self, intent, *, delivery_id, expected_delivery_sha256, trusted_channel):
        intent = self._load(intent)
        value = await self.view(intent)
        if (
            value["delivery_id"] != delivery_id
            or value["delivery_sha256"] != expected_delivery_sha256
        ):
            raise PersonalAgentError("not_found", "请选择本事项确切的已入箱结果")
        transition = await self.jobs.acknowledge_human_result_inbox_delivery(
            intent["execution_session_id"],
            delivery_id,
            expected_delivery_sha256=expected_delivery_sha256,
            trusted_channel=trusted_channel,
        )
        self._bind(intent, acknowledgement_id=transition["receipt"]["acknowledgement_id"])
        return await self.view(intent)
