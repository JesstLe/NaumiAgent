"""Human-approved, exact and single-send research egress over the research journal."""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta

from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.memory_models import normalize_ref as memory_ref
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest
from naumi_agent.personal.research_egress_models import (
    EgressDecide,
    EgressExecute,
    EgressGet,
    EgressList,
    EgressPropose,
    EgressRequest,
    egress_digest,
)
from naumi_agent.personal.research_models import validate_payload

_ID = re.compile(r"^rx_[a-f0-9]{32}$")
_CHANNELS = frozenset({"web", "cli", "tui"})
_BEFORE_SEND = frozenset({"proposed", "approved"})


class ResearchEgressMixin:
    """Persist exact proposals, decisions and send admission without global grants."""

    @staticmethod
    def _egress_fields(model, arguments):
        operation = model.model_fields["operation"].annotation.__args__[0]
        if type(arguments) is not dict:
            raise PersonalAgentError("invalid_input", "外发提案参数格式不正确")
        value = validate_payload(model, {"operation": operation, **arguments})
        return {key: item for key, item in value.items() if item is not None}

    def _egress_principal(self, caller_actor, trusted_channel):
        if caller_actor is None:
            if type(trusted_channel) is not str or trusted_channel not in _CHANNELS:
                raise PersonalAgentError("scope_denied", "请从用户入口管理外发提案")
            return "owner"
        if trusted_channel is not None:
            raise PersonalAgentError("scope_denied", "Agent 调用不能声明人工批准入口")
        return self._actor(caller_actor)

    def _egress_actor_identity(self, actor):
        self._actor(actor)
        if actor == "main":
            return 0, "主 Agent"
        profile = self.personal.require_active_profile(actor)
        return profile.version, profile.name

    @staticmethod
    def _egress_expiry(value):
        try:
            expiry = datetime.fromisoformat(value["expires_at"])
            if expiry.tzinfo is None:
                raise ValueError()
            return expiry.astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            raise PersonalAgentError("storage_error", "外发提案的有效期校验未通过") from None

    async def _egress_record(self, identity, caller_actor=None):
        if type(identity) is not str or not _ID.fullmatch(identity):
            raise PersonalAgentError("invalid_input", "外发提案编号格式不正确")
        value = await self._record("egress", identity)
        if value["workspace_key"] != self.workspace_key or (
            caller_actor is not None and value["actor_id"] != caller_actor
        ):
            raise PersonalAgentError("not_found", "原外发提案不存在")
        if value.get("digest") != egress_digest(value):
            raise self._broken()
        return value

    async def _egress_dependencies(self, value, *, original=False):
        actor = value["actor_id"]
        version, _ = self._egress_actor_identity(actor)
        if original and version != value["actor_version"]:
            raise PersonalAgentError(
                "approval_invalidated", "原 Agent 配置已变化，请重新提出外发提案"
            )
        if await self.artifacts.session_actor(value["session_id"], write=original) != actor:
            raise PersonalAgentError("binding_conflict", "原外发提案的对话身份已变化")
        context = value if original else value["output_context"]
        await self.artifacts.authorize_refs(actor_id=actor, refs=context["artifact_refs"])
        refs = context["memory_refs"]
        if refs:
            memory = getattr(self.engine, "personal_memory", None)
            if memory is None:
                raise PersonalAgentError("source_unavailable", "原外发提案的记忆来源不可用")
            for ref in refs:
                await memory.resolve_ref(ref, actor, item_id=context["memory_item_id"])

    async def _egress_context(self, actor, notebook_id):
        session = getattr(self.engine, "_session", None)
        if session is None or await self.artifacts.session_actor(session.id, write=True) != actor:
            raise PersonalAgentError("binding_conflict", "请从原 Agent 的日常对话提出外发提案")
        session_id = session.id
        raw = [
            *await self.artifacts.session_refs(session_id, actor),
            *getattr(self.engine, "_personal_artifact_refs", []),
            *getattr(self.engine, "_personal_memory_artifact_refs", []),
        ]
        artifact_refs = normalize_refs(list({canonical(ref): ref for ref in raw}.values()))
        memories = getattr(self.engine, "_personal_memory_runtime_refs", [])
        if not isinstance(memories, list) or len(memories) > 64:
            raise PersonalAgentError("input_limit", "本轮记忆来源超过外发提案上限")
        memory_refs = list({canonical(ref): memory_ref(ref) for ref in memories}.values())
        runtime = getattr(self.engine, "personal_memory_runtime", None)
        memory_item_id = runtime.item_id() if runtime is not None else None
        context = {
            "actor_id": actor,
            "actor_version": self._egress_actor_identity(actor)[0],
            "session_id": session_id,
            "artifact_refs": artifact_refs,
            "memory_refs": memory_refs,
            "memory_item_id": memory_item_id,
        }
        # Pin implicit material too, then reuse the existing own-notebook mirror.
        # The original exact set remains the dispatch dependency; only output
        # material drops its mutable self dependency, never the original ACLs.
        await self.artifacts.bind_refs(session_id, actor, artifact_refs)
        output_refs = await self._context_refs(actor, True, notebook_id)
        if self.engine._session is not session:
            raise PersonalAgentError("binding_conflict", "提出外发提案期间对话已变化")
        context["output_context"] = {
            "artifact_refs": output_refs,
            "memory_refs": memory_refs,
            "memory_item_id": memory_item_id,
        }
        await self._egress_dependencies(context, original=True)
        return context

    async def _egress_plan(self, action, payload):
        from naumi_agent.personal.research_capture import build_egress_plan

        plan = build_egress_plan(action, payload, self.engine)
        return await plan if inspect.isawaitable(plan) else plan

    async def _egress_validate(self, value):
        if value.get("digest") != egress_digest(value):
            raise self._broken()
        await self._egress_dependencies(value, original=True)
        if self._egress_expiry(value) <= self.now():
            raise PersonalAgentError("action_expired", "外发提案已过期，请重新核对后提出")
        notebook = await self._notebook(value["notebook_id"], value["actor_id"])
        if (
            notebook["revision"] != value["notebook_revision"]
            or digest(notebook) != value["notebook_digest"]
        ):
            raise PersonalAgentError("source_stale", "原研究已变化，请重新核对外发内容")
        basis, _ = await self._basis(notebook, require_current=True)
        if digest(basis) != digest(value["research_basis"]):
            raise PersonalAgentError("source_stale", "原研究来源或有效期已变化，请重新提出外发提案")
        from naumi_agent.personal.research_capture import validate_egress_plan

        checked = validate_egress_plan(
            value["action"], value["payload"], self.engine, value["destination_plan"]
        )
        if inspect.isawaitable(checked):
            await checked
        return value

    def _egress_current_tx(self, tx, value, *, expiry=True):
        if value.get("digest") != egress_digest(value):
            raise self._broken()
        version, _ = self._egress_actor_identity(value["actor_id"])
        if version != value["actor_version"]:
            raise PersonalAgentError("approval_invalidated", "原 Agent 配置已变化，外发批准已失效")
        if expiry and self._egress_expiry(value) <= self.now():
            raise PersonalAgentError("action_expired", "外发提案已过期，未发起外部请求")
        notebook = tx.get("notebook", value["notebook_id"])
        if digest(notebook) != value["notebook_digest"]:
            raise PersonalAgentError("source_stale", "原研究在批准期间已变化")
        basis = value["research_basis"]
        sets = {value["notebook_id"]: set()}
        for upstream in basis["upstream"]:
            parent = tx.get("notebook", upstream["notebook_id"], revision=upstream["revision"])
            if digest(parent) != upstream["record_digest"]:
                raise self._broken()
            sets.setdefault(parent["id"], set())
        for ref in basis["sources"]:
            exact = tx.get("source", ref["id"], revision=ref["revision"])
            latest = tx.get("source", ref["id"])
            if digest(exact) != ref["digest"]:
                raise self._broken()
            if digest(latest) != ref["latest_digest"] or self._expired(latest) != ref["expired"]:
                raise PersonalAgentError("source_stale", "原来源在批准期间已变化")
            sets.setdefault(latest["notebook_id"], set()).add(latest["id"])
        for identity, expected in sets.items():
            actual = {row["id"] for row in tx.list("source", parent=identity, limit=65)}
            if actual != expected:
                raise PersonalAgentError("source_stale", "原来源集合在批准期间已变化")

    @staticmethod
    def _egress_opaque(value):
        return {
            "proposal": {
                "id": value["id"],
                "revision": value["revision"],
                "digest": "",
                "status": "unavailable",
                "actor_id": value["actor_id"],
                "actor_name": "",
                "actor_version": value["actor_version"],
                "session_id": "",
                "notebook_id": value["notebook_id"],
                "notebook_revision": value["notebook_revision"],
                "notebook_title": "来源不可用",
                "action": value["action"],
                "payload": {},
                "destination_plan": {},
                "reason": "",
                "created_at": value["created_at"],
                "expires_at": value["expires_at"],
                "approval": None,
                "execution": None,
                "dependencies": {},
            },
            "availability": "source_unavailable",
        }

    async def _egress_view(self, value, caller_actor=None, *, include_result=True):
        try:
            await self._egress_dependencies(value)
            status = value["status"]
            if status in _BEFORE_SEND:
                try:
                    await self._egress_validate(value)
                except PersonalAgentError as exc:
                    if exc.code == "action_expired":
                        status = "expired"
                    elif exc.code in {"source_stale", "approval_invalidated"}:
                        status = "stale"
                    else:
                        raise
            public = {
                key: value[key]
                for key in (
                    "id",
                    "revision",
                    "digest",
                    "actor_id",
                    "actor_name",
                    "actor_version",
                    "session_id",
                    "notebook_id",
                    "notebook_revision",
                    "notebook_title",
                    "action",
                    "payload",
                    "destination_plan",
                    "reason",
                    "created_at",
                    "expires_at",
                    "approval",
                )
            }
            execution = value["execution"]
            public["execution"] = (
                None
                if execution is None
                else {
                    key: execution[key] for key in ("request_id", "status", "started_at", "result")
                }
            )
            if execution is not None:
                original = await self._record("intent", execution["intent_id"])
                phase = original["phase"]
                if phase not in {"started", "captured", "completed"}:
                    raise self._broken()
                public["execution"]["phase"] = phase
                public["execution"]["saved_stage"] = phase in {"captured", "completed"}
            public["status"] = status
            public["dependencies"] = {
                "artifact_count": len(value["artifact_refs"]),
                "memory_count": len(value["memory_refs"]),
                "source_count": len({ref["id"] for ref in value["research_basis"]["sources"]}),
                "notebook_revision": value["notebook_revision"],
            }
            body = {"proposal": public, "availability": "available"}
            if include_result and value["status"] == "completed":
                body["result"] = await self._result(execution["result"], value["actor_id"])
            if caller_actor is not None:
                body = await self._pin_egress_result(body, value, caller_actor)
            return body
        except PersonalAgentError as exc:
            if exc.code in {"storage_error", "invalid_input", "input_limit"}:
                raise
            return self._egress_opaque(value)

    async def egress_propose(self, arguments, *, caller_actor=None, trusted_channel=None):
        principal = self._egress_principal(caller_actor, trusted_channel)
        args = self._egress_fields(EgressPropose, arguments)
        actor = self._actor(caller_actor)
        notebook = await self._notebook(args["notebook_id"], actor)
        intent = await self._begin("egress_propose", args, principal, notebook_id=notebook["id"])
        if intent["status"] == "completed":
            value = await self._egress_record(intent["result"]["egress_id"], caller_actor)
            return await self._egress_view(value, caller_actor)
        if notebook["revision"] != args["expected_notebook_revision"]:
            raise PersonalAgentError("version_conflict", "研究已更新，请核对原版本后提出外发提案")
        action = args["action"]
        if action == "search":
            payload = {
                "query": args["query"].strip(),
                "provider": args.get("provider") or "duckduckgo",
                "max_results": args.get("max_results") or 5,
            }
        elif action == "capture":
            payload = {"url": args["url"], "max_age_days": args.get("max_age_days") or 30}
        else:
            source = await self._record("source", args["source_id"])
            if source["notebook_id"] != notebook["id"] or source["actor_id"] != actor:
                raise PersonalAgentError("not_found", "原复查来源不存在")
            if source["revision"] != args["expected_source_revision"]:
                raise PersonalAgentError("version_conflict", "来源已更新，请核对原版本")
            if source["origin"] != "url":
                raise PersonalAgentError("invalid_input", "这份资料没有可复查的原网页")
            payload = {
                "source_id": source["id"],
                "expected_source_revision": source["revision"],
                "url": source["url"],
                "max_age_days": source["max_age_days"],
            }
        plan = await self._egress_plan(action, payload)
        if action != "search":
            payload["url"] = plan["endpoint"]
        context = await self._egress_context(actor, notebook["id"])
        basis, _ = await self._basis(notebook, require_current=True)
        identity = "rx_" + intent["id"][3:]
        value = {
            "id": identity,
            "proposal_revision": 1,
            "revision": 1,
            "status": "proposed",
            "workspace_key": self.workspace_key,
            "actor_id": actor,
            "actor_version": context["actor_version"],
            "actor_name": self._egress_actor_identity(actor)[1],
            "session_id": context["session_id"],
            "notebook_id": notebook["id"],
            "notebook_revision": notebook["revision"],
            "notebook_title": notebook["title"],
            "notebook_digest": digest(notebook),
            "action": action,
            "payload": payload,
            "destination_plan": plan,
            "reason": args["reason"],
            "research_basis": basis,
            "artifact_refs": context["artifact_refs"],
            "memory_refs": context["memory_refs"],
            "memory_item_id": context["memory_item_id"],
            "output_context": context["output_context"],
            "created_at": intent["created_at"],
            "expires_at": (
                datetime.fromisoformat(intent["created_at"])
                + timedelta(minutes=args["expires_in_minutes"])
            ).isoformat(),
            "approval": None,
            "execution": None,
            "intent_request_id": "egress-" + identity,
        }
        value["digest"] = egress_digest(value)
        await self._egress_validate(value)

        def create(tx):
            self._egress_current_tx(tx, value)
            tx.put("egress", value, expected_revision=0)
            return {"egress_id": identity}

        receipt = await self._complete(intent, create)
        return await self._egress_view(
            await self._egress_record(receipt["egress_id"], caller_actor), caller_actor
        )

    async def egress_get(self, arguments, *, caller_actor=None, trusted_channel=None):
        self._egress_principal(caller_actor, trusted_channel)
        args = self._egress_fields(EgressGet, arguments)
        return await self._egress_view(
            await self._egress_record(args["id"], caller_actor), caller_actor
        )

    async def egress_list(self, arguments, *, caller_actor=None, trusted_channel=None):
        self._egress_principal(caller_actor, trusted_channel)
        args = self._egress_fields(EgressList, arguments)
        limit, notebook_id = args["limit"], args.get("notebook_id")

        def visible(tx):
            values, cursor = [], args.get("after")
            while tx is not None:
                rows = tx.list("egress", limit=256, after=cursor, parent=notebook_id)
                for value in rows:
                    if value["workspace_key"] == self.workspace_key and (
                        caller_actor is None or value["actor_id"] == caller_actor
                    ):
                        if value["digest"] != egress_digest(value):
                            raise self._broken()
                        values.append(value)
                        if len(values) > limit:
                            return values
                if len(rows) < 256:
                    break
                cursor = rows[-1]["id"]
            return values

        values = await self._read(visible)
        return {
            "proposals": [
                await self._egress_view(value, caller_actor, include_result=False)
                for value in values[:limit]
            ],
            "next_after": values[limit - 1]["id"] if len(values) > limit else None,
        }

    async def egress_request(self, arguments, *, caller_actor=None, trusted_channel=None):
        principal = self._egress_principal(caller_actor, trusted_channel)
        args = self._egress_fields(EgressRequest, arguments)
        intent = await self._record(
            "intent", self._request_identity(principal, args["request_id"]), missing=True
        )
        response = {"status": "unknown", "request_id": args["request_id"]}
        if intent is None:
            return response
        if intent["actor_id"] != principal or intent["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "原外发请求不存在")
        if not intent["operation"].startswith("egress_"):
            raise PersonalAgentError("idempotency_conflict", "原请求编号已经绑定其他研究操作")
        identity = (
            intent["result"]["egress_id"]
            if intent["status"] == "completed"
            else intent["arguments"].get("id")
        )
        if identity is not None:
            value = await self._egress_record(identity, caller_actor)
            if intent["status"] == "completed" or value["status"] == "completed":
                return {
                    **response,
                    "status": "completed",
                    "view": await self._egress_view(value, caller_actor),
                }
            if value["status"] == "started":
                original = await self._record("intent", value["execution"]["intent_id"])
                if original["phase"] != "captured":
                    return response
                view = await self._egress_view(value, caller_actor)
                pending = {**response, "status": "pending", "view": view}
                if intent["operation"] == "egress_execute" and view["availability"] == "available":
                    accepted = self._egress_fields(EgressExecute, intent["arguments"])
                    if (
                        accepted["id"] != value["id"]
                        or accepted["expected_digest"] != value["digest"]
                    ):
                        raise self._broken()
                    # Resume only the exact original local operation. The head
                    # revision changes at send admission, so substituting it
                    # would mutate the existing request fingerprint. No query,
                    # URL, context references or fresh network arguments leave
                    # this recovery response, and GET never submits the resume.
                    pending["resume"] = {
                        "operation": "egress_execute",
                        "arguments": {
                            key: accepted[key]
                            for key in ("id", "request_id", "expected_revision", "expected_digest")
                        },
                    }
                return pending
        return {**response, "status": "pending"}

    async def egress_decide(self, arguments, *, caller_actor=None, trusted_channel=None):
        principal = self._egress_principal(caller_actor, trusted_channel)
        if caller_actor is not None:
            raise PersonalAgentError("scope_denied", "外发批准、拒绝和撤销需要用户确认")
        args = self._egress_fields(EgressDecide, arguments)
        original = await self._egress_record(args["id"])
        intent = await self._begin(
            "egress_decide", args, principal, notebook_id=original["notebook_id"]
        )
        if intent["status"] == "completed":
            return await self._egress_view(await self._egress_record(intent["result"]["egress_id"]))
        if args["decision"] == "approve":
            await self._egress_validate(original)

        def decide(tx):
            current = tx.get("egress", original["id"])
            if (
                current["revision"] != args["expected_revision"]
                or current["digest"] != args["expected_digest"]
            ):
                raise PersonalAgentError("version_conflict", "您核对的外发提案已变化，请重新打开")
            if current["status"] not in _BEFORE_SEND or current["execution"] is not None:
                raise PersonalAgentError("action_locked", "原外发提案已进入发送阶段，请核对原请求")
            if args["decision"] == "approve":
                if current["status"] != "proposed":
                    raise PersonalAgentError("action_locked", "原提案已经批准，未重复签发批准")
                self._egress_current_tx(tx, current)
            decision = {
                "id": "rxd_" + intent["id"][3:],
                "revision": 1,
                "notebook_id": current["notebook_id"],
                "workspace_key": self.workspace_key,
                "actor_id": current["actor_id"],
                "egress_id": current["id"],
                "proposal_revision": current["proposal_revision"],
                "digest": current["digest"],
                "decision": args["decision"],
                "channel": trusted_channel,
                "created_at": self.stamp(),
            }
            tx.put("egress_decision", decision, expected_revision=0)
            status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[
                args["decision"]
            ]
            updated = {**current, "status": status}
            if args["decision"] == "approve":
                updated["approval"] = {
                    "decision_id": decision["id"],
                    "digest": current["digest"],
                    "proposal_revision": current["proposal_revision"],
                    "approved_at": decision["created_at"],
                    "channel": trusted_channel,
                }
            tx.put("egress", updated, expected_revision=current["revision"])
            return {"egress_id": current["id"]}

        receipt = await self._complete(intent, decide)
        return await self._egress_view(await self._egress_record(receipt["egress_id"]))

    @staticmethod
    def _egress_execution_arguments(value):
        payload = value["payload"]
        arguments = {"request_id": value["intent_request_id"]}
        if value["action"] == "search":
            return {
                **arguments,
                "id": value["notebook_id"],
                "query": payload["query"],
                "max_results": payload["max_results"],
            }
        if value["action"] == "capture":
            return {
                **arguments,
                "id": value["notebook_id"],
                "url": payload["url"],
                "max_age_days": payload["max_age_days"],
            }
        return {
            **arguments,
            "source_id": payload["source_id"],
            "expected_revision": payload["expected_source_revision"],
        }

    async def _start_approved(self, intent, approved_record):
        await self._egress_validate(approved_record)
        holder = getattr(self, "_research_egress_holder", None)
        if holder is None:
            holder = self._research_egress_holder = "research-egress-" + uuid.uuid4().hex

        def start(tx):
            current_intent = tx.get("intent", intent["id"])
            current = tx.get("egress", approved_record["id"])
            if current_intent["status"] == "completed":
                raise PersonalAgentError("request_pending", "原外发请求已有完成结果，请读取原请求")
            if (
                current_intent["revision"] != intent["revision"]
                or current_intent["phase"] != "reserved"
                or current_intent["operation"] != current["action"]
                or current_intent["arguments"] != self._egress_execution_arguments(current)
                or current_intent["actor_id"] != current["actor_id"]
            ):
                raise PersonalAgentError(
                    "request_pending", "原外发请求已经进入发送阶段，未重复发送"
                )
            if current["status"] != "approved" or current["execution"] is not None:
                raise PersonalAgentError("approval_invalidated", "原外发提案没有可用的一次性批准")
            approval = current["approval"]
            if (
                not approval
                or approval["digest"] != current["digest"]
                or approval["proposal_revision"] != current["proposal_revision"]
            ):
                raise PersonalAgentError("approval_invalidated", "原外发批准与确切内容不一致")
            decision = tx.get("egress_decision", approval["decision_id"], revision=1)
            if (
                decision["decision"] != "approve"
                or decision["digest"] != current["digest"]
                or decision["egress_id"] != current["id"]
            ):
                raise self._broken()
            self._egress_current_tx(tx, current)
            lease = tx.claim(
                "egress_dispatch",
                current["id"],
                holder,
                now=self.now().timestamp(),
                ttl=90,
                once=True,
            )
            tx.require_lease(
                "egress_dispatch",
                current["id"],
                holder,
                lease["fence"],
                now=self.now().timestamp(),
                phase="sent",
            )
            execution = {
                "request_id": current["intent_request_id"],
                "intent_id": current_intent["id"],
                "status": "started",
                "started_at": self.stamp(),
                "result": None,
                "holder": holder,
                "fence": lease["fence"],
            }
            tx.put(
                "egress",
                {**current, "status": "started", "execution": execution},
                expected_revision=current["revision"],
            )
            updated_intent = {
                **current_intent,
                "revision": current_intent["revision"] + 1,
                "phase": "started",
                "stage": None,
            }
            tx.put("intent", updated_intent, expected_revision=current_intent["revision"])
            return updated_intent

        updated = await self._write(start)
        # Admission consumes the one-shot before another await. A dependency
        # revoked immediately after admission still stops transport, without
        # ever making that consumed nonce reusable after an unknown result.
        await self._egress_validate(approved_record)
        return updated

    def _finish_egress(self, tx, identity, receipt):
        current = tx.get("egress", identity)
        if current["status"] == "completed":
            if current["execution"]["result"] != receipt:
                raise self._broken()
            return current
        if current["status"] != "started" or not current["execution"]:
            raise PersonalAgentError("approval_invalidated", "原外发提案没有发送凭据")
        execution = current["execution"]
        intent = tx.get("intent", execution["intent_id"])
        lease = tx.lease("egress_dispatch", identity)
        if (
            intent["phase"] != "captured"
            or intent["arguments"] != self._egress_execution_arguments(current)
            or not lease
            or lease["fence"] != execution["fence"]
            or lease["holder"] != execution["holder"]
            or lease["phase"] not in {"sent", "unknown"}
            or receipt["notebook_id"] != current["notebook_id"]
            or receipt["notebook_revision"] != current["notebook_revision"]
        ):
            raise PersonalAgentError("binding_conflict", "外发结果与原发送意图不一致")
        updated = {
            **current,
            "status": "completed",
            "execution": {**execution, "status": "completed", "result": receipt},
        }
        tx.put("egress", updated, expected_revision=current["revision"])
        # A captured receipt can finish after process restart and lease expiry.
        # Keep expired sent proof consumed forever; only release a live fence.
        if lease["expires_at"] > self.now().timestamp():
            tx.require_lease(
                "egress_dispatch",
                identity,
                execution["holder"],
                execution["fence"],
                now=self.now().timestamp(),
                phase="finished",
            )
        return updated

    async def egress_execute(self, arguments, *, caller_actor=None, trusted_channel=None):
        principal = self._egress_principal(caller_actor, trusted_channel)
        args = self._egress_fields(EgressExecute, arguments)
        value = await self._egress_record(args["id"], caller_actor)
        intent = await self._begin(
            "egress_execute", args, principal, notebook_id=value["notebook_id"]
        )
        if intent["status"] == "completed":
            return await self._egress_view(
                await self._egress_record(intent["result"]["egress_id"], caller_actor), caller_actor
            )
        if value["digest"] != args["expected_digest"]:
            raise PersonalAgentError("version_conflict", "您核对的外发内容已变化，请重新打开")
        if (
            value["status"] not in {"started", "completed"}
            and value["revision"] != args["expected_revision"]
        ):
            raise PersonalAgentError("version_conflict", "外发提案状态已变化，请重新打开")
        if caller_actor is not None:
            session = getattr(self.engine, "_session", None)
            if session is None or session.id != value["session_id"]:
                raise PersonalAgentError("binding_conflict", "请从原提案的 Agent 对话执行外发")
        if value["status"] != "completed":
            if value["status"] not in {"approved", "started"}:
                raise PersonalAgentError(
                    "approval_required", "请先由用户核对并批准这份确切外发提案"
                )
            if value["status"] == "approved":
                await self._egress_validate(value)
            else:
                await self._egress_dependencies(value)
            fields = self._egress_execution_arguments(value)
            if value["action"] == "search":
                await self.search(fields, caller_actor=value["actor_id"], _approved=value)
            else:
                await self._capture(
                    value["action"], fields, value["actor_id"], None, _approved=value
                )
            value = await self._egress_record(value["id"], caller_actor)
            if value["status"] != "completed":
                raise PersonalAgentError("request_pending", "原外发结果仍待核对，未重复发送")

        def complete(_tx):
            return {"egress_id": value["id"]}

        receipt = await self._complete(intent, complete)
        return await self._egress_view(
            await self._egress_record(receipt["egress_id"], caller_actor), caller_actor
        )
