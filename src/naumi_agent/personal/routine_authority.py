"""Immutable routine provenance and live execution gates over existing authorities."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar

from naumi_agent.daemons.agent_jobs import AgentJobState, agent_job_id_for_request
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest
from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.item_models import normalize_intent, normalize_policy
from naumi_agent.personal.memory_models import normalize_ref as normalize_memory_ref
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_models import (
    ACTOR,
    canonical,
    detached,
    digest,
    fields,
    integer,
    normalize_plan_ref,
    normalize_routine_ref,
    request_id,
    text,
    timestamp,
)

_WALK = ContextVar("personal_routine_authority_walk", default=None)
_SOURCE_FIELDS = {"artifact_refs", "memory_refs", "memory_item_id", "context_session_ids"}
_OCCURRENCE_BINDING = (
    "routine_id",
    "epoch",
    "definition_revision",
    "definition_sha256",
    "actor_id",
    "workspace_key",
)


def _error(code, message):
    return PersonalAgentError(code, message)


@contextmanager
def _walking(service, identity):
    state = _WALK.get()
    reset = None
    if state is None:
        state = {"active": set(), "done": set(), "nodes": 0}
        reset = _WALK.set(state)
    key = (service.owner_key, service.workspace_key, *identity)
    try:
        if key in state["active"]:
            raise _error("storage_error", "周期资料来源出现自依赖，请核对原始来源链")
        cached = key in state["done"]
        if not cached:
            state["nodes"] += 1
            if len(state["active"]) >= 32 or state["nodes"] > 1024:
                raise _error("storage_error", "周期资料来源链超过校验容量")
            state["active"].add(key)
        try:
            yield cached
        except BaseException:
            raise
        else:
            if not cached:
                state["done"].add(key)
        finally:
            if not cached:
                state["active"].discard(key)
    finally:
        if reset is not None:
            _WALK.reset(reset)


def _source_context(definition):
    value = definition.get("source_context")
    fields(value, _SOURCE_FIELDS, _SOURCE_FIELDS)
    artifacts = normalize_refs(value["artifact_refs"])
    raw_memory = value["memory_refs"]
    if not isinstance(raw_memory, list) or len(raw_memory) > 64:
        raise _error("input_limit", "周期记忆来源最多允许 64 项")
    memories = [normalize_memory_ref(ref) for ref in raw_memory]
    if len({canonical(ref) for ref in memories}) != len(memories):
        raise _error("invalid_input", "周期记忆来源包含重复引用")
    item = value["memory_item_id"]
    if item is not None:
        text(item, "原记忆事项范围", 512)
    sessions = value["context_session_ids"]
    if not isinstance(sessions, list) or len(sessions) > 32:
        raise _error("input_limit", "周期来源对话最多允许 32 项")
    for session in sessions:
        text(session, "原来源对话", 512)
    if len(set(sessions)) != len(sessions):
        raise _error("invalid_input", "周期来源对话包含重复项目")
    return {
        "artifact_refs": artifacts,
        "memory_refs": memories,
        "memory_item_id": item,
        "context_session_ids": list(sessions),
    }


class RoutineAuthorityMixin:
    """Read provenance independently from parent lifecycle; execute only original live work."""

    def _authority_artifacts(self):
        value = getattr(self.engine, "personal_artifacts", None)
        if (
            value is None
            or not callable(getattr(value, "authorize_refs", None))
            or not callable(getattr(value, "_call", None))
        ):
            raise _error("routine_source_revoked", "原周期资料需要当前私有资料授权组件")
        return value

    def _authority_definition(self, value):
        value = detached(value, "原周期定义")
        if not isinstance(value.get("assignee"), dict):
            raise _error("invalid_input", "原周期定义缺少明确的执行 Agent")
        actor = text(value["assignee"].get("id"), "原执行 Agent", 35, pattern=ACTOR)
        version = integer(value["assignee"].get("version"), "原 Agent 版本", minimum=0)
        if (actor == "main") != (version == 0):
            raise _error("binding_conflict", "原周期的 Agent 与版本不一致")
        inputs = value.get("inputs")
        fields(inputs, {"artifact_refs", "directories"}, {"artifact_refs", "directories"})
        normalize_refs(inputs["artifact_refs"])
        if not isinstance(inputs["directories"], list):
            raise _error("invalid_input", "原周期目录来源格式不正确")
        if value.get("route") != self.items._route():
            raise _error("binding_conflict", "原周期定义不属于当前工作区执行路由")
        _source_context(value)
        return value

    async def authorize_definition_sources(self, definition, reader_actor=None):
        """Recheck exact original grants, not current replacement grants or management scope."""
        definition = self._authority_definition(definition)
        actor = definition["assignee"]["id"]
        if reader_actor is not None:
            text(reader_actor, "周期正文读取 Agent", 35, pattern=ACTOR)
        reader = actor if reader_actor is None else reader_actor
        with _walking(self, ("definition", digest(definition), reader)) as cached:
            if cached:
                return
            artifacts = self._authority_artifacts()
            # Input selection is the executor's authority, not a super-reader grant to main.
            await artifacts.authorize_refs(
                actor_id=actor, refs=definition["inputs"]["artifact_refs"]
            )
            sources = _source_context(definition)
            readers = [actor] if reader == actor else [actor, reader]
            memory = getattr(self.engine, "personal_memory", None)
            if sources["memory_refs"] and not callable(getattr(memory, "resolve_ref", None)):
                raise _error("routine_source_revoked", "原周期正文需要当前记忆授权组件")
            for current in readers:
                await artifacts.authorize_refs(actor_id=current, refs=sources["artifact_refs"])
                for ref in sources["memory_refs"]:
                    await memory.resolve_ref(ref, current, item_id=sources["memory_item_id"])

    async def authorize_execution_definition(self, definition):
        """Keep current Agent/policy checks separate from reading historical report sources."""
        definition = self._authority_definition(definition)
        actor = definition["assignee"]["id"]
        if self.actor_version(actor) != definition["assignee"]["version"]:
            raise _error("version_conflict", "周期执行 Agent 的版本已变化，请重新确认后续委托")
        validator = getattr(self.items, "_validate_definition", None)
        if not callable(validator):
            raise _error("storage_error", "周期执行缺少原事项策略校验组件")
        await validator(definition)

    @staticmethod
    def _plan_reference(routine):
        return normalize_plan_ref(
            {
                "routine_id": routine["id"],
                "definition_revision": routine["revision"],
                "definition_sha256": routine["definition_sha256"],
                "actor_id": routine["definition"]["assignee"]["id"],
                "workspace_key": routine["workspace_key"],
            }
        )

    @staticmethod
    def _occurrence_reference(occurrence):
        return normalize_routine_ref(
            {
                "routine_id": occurrence["routine_id"],
                "epoch": occurrence["epoch"],
                "occurrence_id": occurrence["id"],
                "definition_revision": occurrence["definition_revision"],
                "definition_sha256": occurrence["definition_sha256"],
                "actor_id": occurrence["actor_id"],
                "workspace_key": occurrence["workspace_key"],
            }
        )

    async def _authority_original(self, ref):
        if ref["workspace_key"] != self.workspace_key:
            raise _error("routine_source_revoked", "原周期资料不属于当前工作区")
        original = await self._routine(ref["routine_id"], revision=ref["definition_revision"])
        if self._plan_reference(original) != {
            key: ref[key]
            for key in (
                "routine_id",
                "definition_revision",
                "definition_sha256",
                "actor_id",
                "workspace_key",
            )
        }:
            raise _error("routine_source_revoked", "周期资料缺少匹配的原始定义修订")
        if digest(original["definition"]) != ref["definition_sha256"]:
            raise _error("storage_error", "周期资料原始定义摘要不一致")
        return original

    async def _authority_occurrence(self, ref):
        original = await self._authority_original(ref)
        value = await self._record("occurrence", ref["occurrence_id"])
        if self._occurrence_reference(value) != ref or original["epoch"] != ref["epoch"]:
            raise _error("routine_source_revoked", "周期资料缺少匹配的原始发生账本")
        return value, original

    async def authorize_artifact(self, snapshot, actor_id):
        """Walk primitive immutable snapshots; public get() would recursively call this hook."""
        text(actor_id, "周期资料读取 Agent", 35, pattern=ACTOR)
        if not isinstance(snapshot, dict):
            raise _error("storage_error", "周期资料缺少原始产物凭据")
        artifacts = self._authority_artifacts()

        async def visit(value):
            normalize_refs(
                [
                    {
                        "artifact_id": value.get("artifact_id"),
                        "version": value.get("version"),
                        "sha256": value.get("sha256"),
                        "grant_id": value.get("grant_id", ""),
                        "grant_revision": value.get("grant_revision", 0),
                    }
                ]
            )
            identity = (
                "artifact",
                value["artifact_id"],
                value["version"],
                value["sha256"],
                actor_id,
            )
            with _walking(self, identity) as cached:
                if cached:
                    return
                evidence = value.get("evidence")
                if not isinstance(evidence, dict):
                    raise _error("storage_error", "产物来源证据不是有效对象")
                for name, normalizer in (
                    ("routine_refs", normalize_routine_ref),
                    ("routine_plan_refs", normalize_plan_ref),
                ):
                    refs = evidence.get(name, [])
                    if not isinstance(refs, list) or len(refs) > 64:
                        raise _error("storage_error", "周期来源引用超出有效列表容量")
                    for raw in refs:
                        ref = normalizer(raw)
                        if name == "routine_refs":
                            _, original = await self._authority_occurrence(ref)
                        else:
                            original = await self._authority_original(ref)
                        await self.authorize_definition_sources(original["definition"], actor_id)
                for ref in normalize_refs(value.get("source_refs", [])):
                    source = await artifacts._call(
                        "get",
                        actor_id=actor_id,
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
                    if source["sha256"] != ref["sha256"]:
                        raise _error("routine_source_revoked", "周期上游资料与原版本摘要不一致")
                    await visit(source)

        await visit(snapshot)

    async def _authority_current_session(self, actor, expected=None):
        session = getattr(self.engine, "_session", None)
        if session is None or expected is not None and session is not expected:
            raise _error("binding_conflict", "周期正文读取需要当前 Agent 的原有效对话")
        if getattr(session, "personal_agent_id", None) != ("" if actor == "main" else actor):
            raise _error("binding_conflict", "周期正文读取与当前对话 Agent 不一致")
        text(getattr(session, "id", None), "当前对话编号", 512)
        artifacts = self._authority_artifacts()
        if not callable(getattr(artifacts, "session_actor", None)) or (
            await artifacts.session_actor(session.id, write=True) != actor
        ):
            raise _error("binding_conflict", "周期正文读取与原持久对话归属不一致")
        return session

    async def pin_plan_read(self, public_routine, actor):
        """Pin every model-visible plan body as real provenance-bearing material first."""
        if actor is None:
            return public_routine
        text(actor, "周期正文读取 Agent", 35, pattern=ACTOR)
        version = self.actor_version(actor)
        session = await self._authority_current_session(actor)
        session_id = session.id
        public = detached(public_routine, "周期正文")
        original = await self._routine(public["id"], actor, revision=public["revision"])
        expected_definition = detached(original["definition"])
        expected_definition.pop("source_context", None)
        expected_definition["schedule"].pop("calendar_sessions", None)
        if (
            public["definition_sha256"] != original["definition_sha256"]
            or public["definition"] != expected_definition
            or any(
                key in public and public[key] != original[key]
                for key in (
                    "id",
                    "revision",
                    "epoch",
                    "status",
                    "created_at",
                    "updated_at",
                )
            )
        ):
            raise _error("binding_conflict", "周期正文与原始定义修订不一致")
        await self.authorize_definition_sources(original["definition"], actor)
        ref = self._plan_reference(original)
        sources = _source_context(original["definition"])
        content = canonical(public).encode("utf-8")
        artifacts = self._authority_artifacts()
        snapshot = await artifacts.publish(
            actor_id=actor,
            request_id="routine-plan-read:"
            + digest({"reference": ref, "reader": actor, "body": public}),
            title="周期计划",
            kind="material",
            filename="routine-plan.txt",
            content=content,
            source_refs=sources["artifact_refs"],
            evidence={
                "source_kind": "routine_plan",
                "routine_plan_refs": [ref],
                "source_context": sources,
            },
        )
        if snapshot["sha256"] != hashlib.sha256(content).hexdigest():
            raise _error("storage_error", "周期正文产物缺少匹配的实际字节")
        artifact_ref = artifacts.reference(snapshot)
        await artifacts.bind_refs(session_id, actor, [artifact_ref])
        await self._authority_current_session(actor, session)
        if session.id != session_id or self.actor_version(actor) != version:
            raise _error("binding_conflict", "读取期间对话或 Agent 已变化，周期正文未返回")
        await self.authorize_definition_sources(original["definition"], actor)
        return public_routine

    async def _authority_reservation(self, occurrence):
        value = await self._record("budget", "reservation:" + occurrence["id"])
        if (
            value.get("routine_id") != occurrence["routine_id"]
            or value.get("occurrence_id") != (occurrence["id"])
        ):
            raise _error("binding_conflict", "原周期发生与预算预留不一致")
        if value.get("settlement") not in {"reserved", "unknown"} or value.get("status") not in {
            "reserved",
            "unknown",
        }:
            raise _error("occurrence_cancelled", "原周期发生的执行预留已经结束")
        integer(value.get("cost_microusd"), "原单次预留额度")
        return value

    async def create_occurrence_guard(self, occurrence, routine_snapshot):
        """Wrap the first actual input, preserving capacity and all original context taints."""
        ref = self._occurrence_reference(occurrence)
        ledger, original = await self._authority_occurrence(ref)
        await self._authority_reservation(ledger)
        if (
            self._plan_reference(routine_snapshot) != self._plan_reference(original)
            or routine_snapshot["epoch"] != original["epoch"]
            or routine_snapshot["definition"] != original["definition"]
        ):
            raise _error("binding_conflict", "周期执行资料与预留时的原始定义不一致")
        definition, actor = original["definition"], ref["actor_id"]
        await self.authorize_execution_definition(definition)
        await self.authorize_definition_sources(definition, actor)
        artifacts = self._authority_artifacts()
        inputs = normalize_refs(definition["inputs"]["artifact_refs"])
        sources = _source_context(definition)
        ancestors = list(sources["artifact_refs"])
        if inputs:
            first = inputs[0]
            snapshot, content = await artifacts.read(
                actor_id=actor,
                artifact_id=first["artifact_id"],
                version=first["version"],
            )
            if (
                snapshot["sha256"] != first["sha256"]
                or hashlib.sha256(content).hexdigest() != (first["sha256"])
            ):
                raise _error("routine_source_revoked", "原输入字节与周期委托的确切版本不一致")
            title, filename = snapshot["title"], "routine-source" + snapshot["suffix"]
            ancestors.append(first)
        else:
            title, filename = "周期执行凭据", "routine-guard.txt"
            content = canonical(
                {"type": "routine_occurrence_guard", "routine_ref": ref, "source_context": sources}
            ).encode("utf-8")
        ancestors = normalize_refs(list({canonical(value): value for value in ancestors}.values()))
        snapshot = await artifacts.publish(
            actor_id=actor,
            request_id="routine-guard:" + digest(ref),
            title=title,
            kind="material",
            filename=filename,
            content=content,
            source_refs=ancestors,
            evidence={
                "source_kind": "routine_occurrence_guard",
                "routine_refs": [ref],
                "source_context": sources,
            },
        )
        if snapshot["sha256"] != hashlib.sha256(content).hexdigest():
            raise _error("storage_error", "周期执行资料没有保存原始实际字节")
        guard = artifacts.reference(snapshot)
        child_inputs = normalize_refs(([guard] + inputs[1:]) if inputs else [guard])
        await self.authorize_artifact(snapshot, actor)
        await self.authorize_execution_definition(definition)
        await self.authorize_definition_sources(definition, actor)
        return guard, child_inputs

    async def _authority_item(self, ledger, original, intent, reservation):
        identity = request_id(ledger.get("item_request_id"))
        receipt = await self.items._call("find_request", request_id=identity)
        if receipt is None:
            raise _error("storage_error", "原子事项创建回执尚未确认，请核对原请求，不重新创建")
        committed, current = receipt.get("committed_item"), receipt.get("item")
        if (
            not isinstance(committed, dict)
            or not isinstance(current, dict)
            or (receipt.get("operation") != "create" or receipt.get("request_id") != identity)
        ):
            raise _error("binding_conflict", "周期执行缺少原始创建回执")
        item_id = receipt.get("item_id")
        if (
            item_id != committed.get("item_id")
            or item_id != current.get("item_id")
            or item_id != intent.get("item_id")
            or ledger.get("item_id") is not None
            and ledger["item_id"] != item_id
        ):
            raise _error("binding_conflict", "周期发生与原子事项编号不一致")
        self.items._scope(committed)
        self.items._scope(current)
        if (
            intent.get("item_revision") != committed["revision"]
            or intent.get("once_key") != committed["once_key"]
            or intent.get("definition_sha256") != committed["definition_sha256"]
            or digest(committed["definition"]) != committed["definition_sha256"]
            or current["once_key"] != committed["once_key"]
            or current["definition_sha256"] != committed["definition_sha256"]
            or current["state"] != "active"
        ):
            raise _error("binding_conflict", "周期发生的原单次委托已经改变或结束")
        definition, child = original["definition"], committed["definition"]
        if (
            any(
                child.get(key) != definition.get(key)
                for key in (
                    "instructions",
                    "assignee",
                    "route",
                )
            )
            or child["inputs"]["directories"] != definition["inputs"]["directories"]
        ):
            raise _error("binding_conflict", "原子事项的正文、Agent 或目录与周期定义不一致")
        policy = normalize_policy(child["policy"])
        expected_policy = normalize_policy(definition["policy"])
        if expected_policy["max_cost_microusd"] is None:
            raise _error("binding_conflict", "原周期缺少有限的单次预算策略")
        if any(
            policy[key] != expected_policy[key] for key in policy if key != "max_cost_microusd"
        ) or (
            policy["max_cost_microusd"] is None
            or policy["max_cost_microusd"] > expected_policy["max_cost_microusd"]
            or policy["max_cost_microusd"] > reservation["cost_microusd"]
            or intent.get("policy") != policy
        ):
            raise _error("binding_conflict", "原子事项的执行策略超过原周期预留")
        guard = normalize_refs([ledger.get("guard_ref")])[0]
        child_refs = normalize_refs(ledger.get("child_input_refs"))
        original_refs = normalize_refs(definition["inputs"]["artifact_refs"])
        expected_refs = normalize_refs([guard] + original_refs[1:]) if original_refs else [guard]
        if (
            child_refs != expected_refs
            or child["inputs"]["artifact_refs"] != child_refs
            or not {canonical(ref) for ref in child_refs}
            <= {canonical(ref) for ref in normalize_refs(intent.get("input_refs"))}
            or intent.get("actor_id") != ledger["actor_id"]
            or intent.get("agent_version") != definition["assignee"]["version"]
            or intent.get("workspace_key") != ledger["workspace_key"]
        ):
            raise _error("binding_conflict", "原子事项缺少原发生的资料与执行身份绑定")
        await self._authority_artifacts().authorize_refs(
            actor_id=ledger["actor_id"], refs=child_refs
        )
        guard_snapshot = await self._authority_artifacts()._call(
            "get",
            actor_id=ledger["actor_id"],
            artifact_id=guard["artifact_id"],
            version=guard["version"],
        )
        refs = guard_snapshot.get("evidence", {}).get("routine_refs")
        if refs != [self._occurrence_reference(ledger)]:
            raise _error("binding_conflict", "原子事项执行凭据没有绑定该真实发生")
        sources = _source_context(definition)
        ancestors = [*sources["artifact_refs"], *original_refs[:1]]
        ancestors = normalize_refs(list({canonical(ref): ref for ref in ancestors}.values()))
        if (
            guard_snapshot.get("owner_id") != ledger["actor_id"]
            or guard_snapshot.get("kind") != "material"
            or guard_snapshot.get("source_refs") != ancestors
            or guard_snapshot.get("evidence", {}).get("source_context") != sources
        ):
            raise _error("binding_conflict", "原周期执行凭据丢失了确切来源上下文")
        expected_sha = (
            original_refs[0]["sha256"]
            if original_refs
            else hashlib.sha256(
                canonical(
                    {
                        "type": "routine_occurrence_guard",
                        "routine_ref": self._occurrence_reference(ledger),
                        "source_context": sources,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        if guard_snapshot["sha256"] != expected_sha:
            raise _error("binding_conflict", "周期执行凭据没有保留原始输入字节")

    async def _authority_running(self, intent):
        try:
            values = dict(intent["request"])
            values["tool_scope"] = tuple(values["tool_scope"])
            request = AgentWorkerRequest(**values)
        except (KeyError, ValueError, TypeError) as exc:
            raise _error("binding_conflict", "周期执行缺少完整原始 Job 请求") from exc
        if request.agent_name != intent["actor_id"]:
            raise _error("binding_conflict", "原 Job 请求的 Agent 与执行意图不一致")
        jobs = getattr(getattr(self.items, "executor", None), "jobs", None)
        if not callable(getattr(jobs, "get", None)):
            raise _error("storage_error", "周期执行需要原 Job 权威，不以界面状态代替真实运行")
        identity = agent_job_id_for_request(request)
        references = intent.get("references", {})
        if references.get("job_id") not in {None, identity}:
            raise _error("binding_conflict", "周期执行与原 Job 编号不一致")
        job = await jobs.get(identity)
        if job is None:
            if references.get("job_id"):
                raise _error("storage_error", "原已受理 Job 缺失，执行状态保持待核对")
            return False
        if job.request != request:
            raise _error("binding_conflict", "周期执行 Job 与原请求不一致")
        if job.state is AgentJobState.RUNNING:
            if (
                not job.claim_owner_id
                or type(job.claim_epoch) is not int
                or job.claim_epoch <= 0
                or job.claim_expires_at is None
                or timestamp(job.claim_expires_at) <= self.now()
            ):
                raise _error("lease_expired", "原执行租约已经失效，请沿原记录核对，不重新执行")
            return True
        if job.state not in {AgentJobState.ADMITTED, AgentJobState.CLAIMED}:
            raise _error("occurrence_ended", "原 Job 已明确结束或待核对，不作为新的执行受理")
        if job.state is AgentJobState.CLAIMED and (
            job.claim_expires_at is None or timestamp(job.claim_expires_at) <= self.now()
        ):
            raise _error("lease_expired", "原待启动租约已经失效，请沿原请求恢复，不新建执行")
        return False

    async def authorize_occurrence_execution(self, occurrence, intent):
        """Authorize the original Item/request, then fence current parent and Job boundaries."""
        ref = self._occurrence_reference(occurrence)
        ledger, original = await self._authority_occurrence(ref)
        if (
            any(
                occurrence.get(key) != ledger.get(key)
                for key in (
                    *_OCCURRENCE_BINDING,
                    "id",
                    "item_request_id",
                    "guard_ref",
                    "child_input_refs",
                )
            )
            or occurrence.get("item_id") is not None
            and occurrence["item_id"] != intent.get("item_id")
        ):
            raise _error("binding_conflict", "执行所用发生记录与原持久账本不一致")
        checked = normalize_intent(intent)
        stored = await self.items._call("get_intent", occurrence_id=checked["occurrence_id"])
        if normalize_intent(stored) != checked:
            raise _error("binding_conflict", "周期执行意图与原事项持久凭据不一致")
        intent = stored
        reservation = await self._authority_reservation(ledger)
        await self._authority_item(ledger, original, intent, reservation)
        await self.authorize_execution_definition(original["definition"])
        await self.authorize_definition_sources(original["definition"], ledger["actor_id"])
        running = await self._authority_running(intent)
        current = await self._routine(ledger["routine_id"])
        if current["status"] not in {"active", "paused", "cancelled", "closed"}:
            raise _error("storage_error", "原周期状态缺少可核验的生命周期值")
        if current["status"] in {"cancelled", "closed"}:
            raise _error("occurrence_cancelled", "原周期已经结束，当前执行在下一边界停止")
        if not running and (current["status"] == "paused" or current["epoch"] != ledger["epoch"]):
            raise _error(
                "occurrence_paused" if current["status"] == "paused" else "occurrence_superseded",
                "原周期已暂停或后续规则已修订，尚未开始的原执行未继续启动",
            )
