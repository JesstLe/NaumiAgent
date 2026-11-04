"""Durable team conversations over staged, existing collaboration Jobs.

The journal records inputs, phase references and delivery facts. Planning releases
its capacity before member Jobs start; summarization has another fenced Job. No
team status column or independent execution lease duplicates AgentJob authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextvars import Context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from naumi_agent.daemons.agent_jobs import AgentJobError
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.peer_models import text
from naumi_agent.personal.team_models import (
    capture_roster,
    normalize_client_payload,
    normalize_plan,
    phase_budget,
    validate_current,
)
from naumi_agent.personal.team_store import TeamTurnStore
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

logger = logging.getLogger(__name__)
_PHASES = ("planning", "members", "summary")
_LIVE = {"waiting", "admitted", "claimed", "running"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass
class TeamExecution:
    service: Any
    turn_id: str
    phase: str
    engine: Any
    plan_id: str
    task_id: str

    async def authorize(self) -> None:
        session = getattr(self.engine, "_session", None)
        if (
            session is None
            or session.personal_execution_id != self.task_id
            or getattr(self.engine, "_personal_collaboration_task_id", "") != self.task_id
        ):
            raise PersonalAgentError("binding_conflict", "团队协调器的执行会话已改变")
        authorize = getattr(self.engine, "_personal_execution_authorizer", None)
        if authorize is None:
            raise PersonalAgentError("execution_fenced", "团队协调器缺少本次执行租约")
        await authorize()
        turn = self.service.store.get(self.turn_id, self.service.workspace_key)
        self.service.require_live(turn)


class PersonalTeamService:
    def __init__(self, engine: Any, *, store: TeamTurnStore | None = None) -> None:
        self.engine = engine
        self.personal = engine.personal_agents
        self.collaborations = engine.personal_collaborations
        self.workspace_key = self.personal.workspace_key
        self.store = store or TeamTurnStore(self.personal.store.base_dir / "teams")
        self._worker: asyncio.Task | None = None
        self._closing = False
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()

    def profile(self, identity: str, version: int):
        for profile in self.personal.history(identity):
            if profile.version == version:
                return profile
        raise PersonalAgentError("version_conflict", "团队任务的原职责版本缺失，请恢复资料库")

    def require_live(self, turn: dict) -> None:
        if turn["cancel_requested"]:
            raise PersonalAgentError("team_cancelled", "团队任务已请求停止，不再执行后续动作")
        validate_current(self.personal, turn["roster"])
        team = self.personal.require_active_profile(turn["team_id"])
        if team.kind != "team" or team.version != turn["team_version"]:
            raise PersonalAgentError("version_conflict", "团队配置已变更，请重新确认任务")
        if turn["policy"]["permission_mode"] != self.engine.config.safety.permission_mode:
            raise PersonalAgentError("version_conflict", "执行权限策略已变更，旧团队任务已停止")

    def events(self, turn: dict) -> list[dict]:
        result, after = [], 0
        while True:
            batch = self.store.events(turn["id"], self.workspace_key, after=after, limit=500)
            result.extend(batch)
            if len(batch) < 500:
                return result
            after = batch[-1]["sequence"]

    def event(self, turn: dict, key: str) -> dict | None:
        return next((event for event in self.events(turn) if event["key"] == key), None)

    def record(self, turn: dict, key: str, kind: str, data: dict) -> dict:
        return self.store.append_event(
            turn["id"], self.workspace_key, key=key, kind=kind, data=data
        )

    @staticmethod
    def phase_request(turn: dict, phase: str) -> str:
        return f"team-{turn['id']}-{phase}"

    async def start_turn(self, *, source_session_id: str, **raw: Any) -> dict:
        client = normalize_client_payload(raw)
        source = await self.engine.session_store.load(source_session_id)
        if source is None:
            raise PersonalAgentError("not_found", "来源对话不存在，请重新打开对话")
        if not self.personal._session_valid(source):
            raise PersonalAgentError("session_conflict", "来源对话已归档，请先恢复对话")
        if source.personal_execution_id:
            raise PersonalAgentError("execution_session_readonly", "请从日常对话发起团队任务")
        self.collaborations.assert_writable(source_session_id)
        marker = source.personal_agent_id
        owner = self.personal.current(source_session_id)
        if marker and (owner is None or owner.id != marker):
            raise PersonalAgentError("binding_conflict", "来源对话的 Agent 归属不一致")
        if owner is not None and (owner.kind != "team" or owner.id != client["team_id"]):
            raise PersonalAgentError("team_scope_denied", "请从主 Agent 或当前团队对话发起任务")
        existing = self.store.find_request(
            self.workspace_key, source_session_id, client["request_id"]
        )
        if existing is not None:
            original = {
                key: value
                for key, value in existing["client_payload"].items()
                if key != "source_context"
            }
            if original != client:
                error = PersonalAgentError(
                    "idempotency_conflict", "原请求已受理，请保留原正文与授权"
                )
                error.accepted, error.turn_id = True, existing["id"]
                raise error
            self.start()
            return await self.get(existing["id"])
        team = self.personal.require_active_profile(client["team_id"])
        if team.kind != "team":
            raise PersonalAgentError("invalid_input", "请选择已创建的团队")
        if team.version != client["team_version"]:
            raise PersonalAgentError("version_conflict", "团队配置已更新，请刷新后重新确认")
        grants = client.get("member_grants")
        if "personal_team" not in team.tool_names:
            if grants:
                raise PersonalAgentError("team_scope_denied", "当前团队未启用成员分工")
            grants = []
        roster = capture_roster(self.personal, team, self.engine.tool_registry, grants)
        source_context = await self._sources(source_session_id, client["source_ids"])
        session = await self.personal.open_session(team.id)
        await self.engine.personal_artifacts.propagate(
            source_session_id, session["session_id"], team.id
        )
        policy = {
            "permission_mode": self.engine.config.safety.permission_mode,
            "max_budget_usd": self.engine.config.safety.max_budget_usd,
            "max_turns": self.engine.config.safety.max_turns,
            "max_input_tokens": self.engine.config.safety.max_input_tokens,
            "max_output_tokens": self.engine.config.safety.max_output_tokens,
        }
        turn = self.store.create(
            workspace_key=self.workspace_key,
            source_session_id=source_session_id,
            team_session_id=session["session_id"],
            team_id=team.id,
            team_version=team.version,
            request_id=client["request_id"],
            client_payload={**client, "source_context": source_context},
            roster=roster,
            policy=policy,
        )
        try:
            await self._append_request(turn)
        except PersonalAgentError as exc:
            exc.accepted, exc.turn_id = True, turn["id"]
            raise
        self.start()
        self._wake.set()
        return await self.get(turn["id"])

    async def _sources(self, session_id: str, identities: list[str]) -> str:
        if not identities:
            return ""
        sources = {s.id: s for s in await self.engine.chat_run_store.list_sources(session_id)}
        root = Path(self.personal.workspace_root).resolve()
        sections = []
        for identity in identities:
            source = sources.get(identity)
            if source is None:
                raise PersonalAgentError("team_scope_denied", "附件来源不属于本次对话")
            if source.kind == "artifact":
                body = await self.engine.personal_artifacts.source_text(
                    session_id=session_id, source=source
                )
                sections.append({"source_id": identity, "title": source.title,
                                 "artifact_ref": source.metadata["artifact_ref"], "content": body,
                                 "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
                continue
            path = (root / source.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise PersonalAgentError("not_found", "附件已移动或不在当前工作区")
            with path.open(encoding="utf-8", errors="replace") as stream:
                body = stream.read(20000)
            sections.append(
                {
                    "source_id": identity,
                    "title": source.title,
                    "path": source.path,
                    "content": body,
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }
            )
        encoded = _json(sections)
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise PersonalAgentError("invalid_input", "本次附件资料较多，请分成几个明确的团队任务")
        return encoded

    def _metadata(self, turn: dict) -> dict:
        return {
            "team_turn_id": turn["id"],
            "team_id": turn["team_id"],
            "team_version": turn["team_version"],
            "source_session_id": turn["source_session_id"],
            "request_id": turn["request_id"],
        }

    async def _append_request(self, turn: dict) -> None:
        try:
            await self.engine.session_store.append_personal_messages_once(
                turn["team_session_id"],
                expected_agent_id=turn["team_id"],
                delivery_id="request-" + turn["id"],
                messages=[
                    {
                        "id": "team-user-" + turn["id"],
                        "role": "user",
                        "content": turn["client_payload"]["content"],
                        "metadata": self._metadata(turn),
                    }
                ],
            )
        except ValueError as exc:
            raise PersonalAgentError("session_conflict", str(exc)) from exc
        self.record(
            turn,
            "request-appended",
            "request_appended",
            {"session_id": turn["team_session_id"], "message_id": "team-user-" + turn["id"]},
        )

    async def phases(self, turn: dict) -> list[dict]:
        result = []
        for phase in _PHASES:
            candidate = self.collaborations.store.find_request(
                self.workspace_key, turn["team_session_id"], self.phase_request(turn, phase)
            )
            if candidate is None:
                continue
            self._validate_binding(turn, candidate, phase)
            result.append(
                {
                    "phase": phase,
                    "collaboration_id": candidate["id"],
                    "collaboration": await self.collaborations.get(candidate["id"]),
                }
            )
        return result

    def _validate_binding(self, turn: dict, plan: dict, phase: str) -> None:
        prepared = self.event(turn, "phase:" + phase)
        if prepared is None:
            raise PersonalAgentError("binding_conflict", "团队阶段缺少原始请求凭据")
        expected = prepared["data"]["request"]
        if (
            plan.get("team_execution") != expected["team_execution"]
            or plan["source_session_id"] != turn["team_session_id"]
            or plan["request_id"] != self.phase_request(turn, phase)
        ):
            raise PersonalAgentError("binding_conflict", "团队阶段与原任务身份不一致")
        fields = ("id", "agent_id", "agent_version", "task", "depends_on", "tool_names")
        actual = [{key: step[key] for key in fields if key in step} for step in plan["steps"]]
        if actual != expected["steps"] or plan["context"] != expected["context"]:
            raise PersonalAgentError("binding_conflict", "团队分工与已确认计划不一致")

    def validate_phase_request(self, owner: dict, **request: Any) -> None:
        turn = self.store.get(owner["turn_id"], self.workspace_key)
        self.require_live(turn)
        prepared = self.event(turn, "phase:" + owner["phase"])
        if prepared is None or _json({**request, "team_execution": owner}) != _json(
            prepared["data"]["request"]
        ):
            raise PersonalAgentError("binding_conflict", "团队阶段不是原任务确认的请求")

    def authorize_plan(self, plan: dict) -> None:
        meta = plan["team_execution"]
        turn = self.store.get(meta["turn_id"], self.workspace_key)
        self.require_live(turn)
        self._validate_binding(turn, plan, meta["phase"])

    async def authorize_execution(self, plan: dict) -> None:
        self.authorize_plan(plan)
        turn = self.store.get(plan["team_execution"]["turn_id"], self.workspace_key)
        session = await self.engine.session_store.load(turn["team_session_id"])
        if (
            session is None
            or session.personal_agent_id != turn["team_id"]
            or session.personal_team_id != turn["team_id"]
            or session.personal_execution_id
            or not self.personal._session_valid(session)
        ):
            raise PersonalAgentError("session_conflict", "原团队对话已停止接收结果，已停止后续动作")

    async def usage(self, phases: list[dict]) -> dict:
        tokens, cost_micro, input_tokens, output_tokens, jobs = 0, 0, 0, 0, set()
        complete = True
        for phase in phases:
            view = phase["collaboration"]
            for step in view["steps"]:
                identity = step["job_id"]
                if not identity or identity in jobs:
                    continue
                jobs.add(identity)
                job = await self.collaborations.jobs.get(identity)
                if job is None or job.result is None:
                    # Live jobs have no settled counters yet. Terminal evidence is
                    # checked separately before advancing to another phase.
                    continue
                tokens += job.result.total_tokens
                cost_micro += job.result.total_cost_microusd
                session_ids = [
                    step["session_id"],
                    *[
                        peer["session_id"]
                        for peer in view.get("peer_sessions", [])
                        if peer["step_id"] == step["id"]
                    ],
                ]
                seen_runs, job_input, job_output = set(), 0, 0
                for session_id in session_ids:
                    if session_id == step["session_id"]:
                        run = (
                            await self.engine.chat_run_store.get_run(session_id, step["run_id"])
                            if step["run_id"]
                            else None
                        )
                    else:
                        records = await self.engine.chat_run_store.list_runs(session_id, limit=1)
                        run = records[0] if records else None
                    if run is None or run.usage is None:
                        complete = False
                    elif run.id not in seen_runs:
                        seen_runs.add(run.id)
                        job_input += run.usage.input_tokens
                        job_output += run.usage.output_tokens
                if job_input + job_output != job.result.total_tokens:
                    complete = False
                input_tokens += job_input
                output_tokens += job_output
        return {
            "total_tokens": tokens,
            "total_cost_usd": cost_micro / 1_000_000,
            "input_tokens": input_tokens if complete else None,
            "output_tokens": output_tokens if complete else None,
            "token_breakdown_complete": complete,
            "job_count": len(jobs),
        }

    @staticmethod
    def _require_token_usage(turn: dict, used: dict) -> None:
        if not used["token_breakdown_complete"] and any(
            turn["policy"][name] is not None
            for name in ("max_input_tokens", "max_output_tokens")
        ):
            raise PersonalAgentError(
                "usage_unknown", "团队阶段的输入或输出用量待核对，已暂停后续阶段"
            )

    async def get(self, turn_id: str) -> dict:
        try:
            return await self._view(self.store.get(turn_id, self.workspace_key))
        except AgentJobError as exc:
            raise PersonalAgentError("storage_error", "团队执行凭据暂不可用，请恢复后重试") from exc

    async def list(self, *, team_session_id: str | None = None, limit: int = 100) -> list[dict]:
        turns = self.store.list(self.workspace_key, team_session_id=team_session_id, limit=limit)
        return [await self.get(turn["id"]) for turn in turns]

    async def _view(self, turn: dict) -> dict:
        phases = await self.phases(turn)
        used = await self.usage(phases)
        by_phase = {item["phase"]: item["collaboration"] for item in phases}
        team = self.profile(turn["team_id"], turn["team_version"])
        delivery = self.event(turn, "delivered")
        status, error, response, result_sha = "queued", "", "", ""
        for phase in _PHASES:
            plan = by_phase.get(phase)
            if plan is None:
                break
            stage = {"planning": "planning", "members": "working", "summary": "summarizing"}[phase]
            status = stage
            if plan["status"] != "completed":
                if plan["status"] not in _LIVE:
                    status = plan["status"]
                    error = next(
                        (s["error"] for s in plan["steps"] if s["error"]), "团队阶段未完成"
                    )
                break
            for step in plan["steps"]:
                run = (
                    await self.engine.chat_run_store.get_run(step["session_id"], step["run_id"])
                    if step["run_id"]
                    else None
                )
                if run is None or run.receipt is None:
                    status, error = "unknown", "团队阶段缺少可信运行回执，请核对原记录"
                    break
                if run.status != "completed" or run.receipt.outcome != "completed":
                    status, error = "error", "团队阶段尚未通过完成核验，请查看运行记录"
                    break
            if status in {"error", "unknown"}:
                break
            proposal = self.event(turn, "plan-proposed")
            if phase == "planning" and proposal is not None:
                step = plan["steps"][0]
                if (
                    proposal["data"]["source_run_id"] != step["run_id"]
                    or proposal["data"]["collaboration_id"] != plan["id"]
                    or proposal["data"]["task_id"] != step["task_id"]
                ):
                    raise PersonalAgentError("binding_conflict", "团队分工缺少原规划运行的可信归属")
                status = "working"
                continue
            if phase == "members":
                status = "summarizing"
                continue
            final_step = plan["steps"][-1]
            response, result_sha = final_step["response"], final_step.get("result_sha256", "")
            status = "completed" if delivery is not None else "delivery_pending"
            break
        if delivery is None:
            rejected = self.event(turn, "execution-rejected")
            if rejected is not None:
                status, error = "blocked", rejected["data"]["message"]
            try:
                self.require_live(turn)
            except PersonalAgentError as exc:
                live = any(p["collaboration"]["status"] in _LIVE for p in phases)
                if turn["cancel_requested"]:
                    status = status if live else "cancelled"
                    error = "正在停止团队执行" if live else "团队任务已停止"
                elif status not in {"unknown", "error", "cancelled"}:
                    status, error = "blocked", exc.message
        error_code = ""
        try:
            self._require_token_usage(turn, used)
        except PersonalAgentError as exc:
            if status not in {"error", "cancelled", "blocked"}:
                status, error, error_code = "unknown", exc.message, exc.code
        return {
            **turn,
            "team_name": team.name,
            "content": turn["client_payload"]["content"],
            "context": turn["client_payload"]["context"],
            "status": status,
            "source_user_message_id": "team-user-" + turn["id"],
            "result_message_id": "team-result-" + turn["id"] if delivery else "",
            "response": response,
            "error": error,
            "error_code": error_code,
            "result_sha256": result_sha,
            "usage": used,
            "phases": phases,
            "events": self.events(turn),
        }

    async def cancel(self, turn_id: str) -> dict:
        current = await self.get(turn_id)
        if current["status"] == "completed":
            return current
        turn = self.store.request_cancel(turn_id, self.workspace_key)
        for phase in await self.phases(turn):
            await self.collaborations.cancel(phase["collaboration_id"])
        self._wake.set()
        self.start()
        return await self.get(turn_id)

    def start(self) -> None:
        if self._closing or getattr(self.engine, "_isolated_child", False):
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._loop(), name="personal-team-turns", context=Context()
            )
        self.collaborations.start()
        self._wake.set()

    async def stop(self) -> None:
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._closing:
            try:
                self._wake.clear()
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Team turn recovery failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        async with self._tick_lock:
            blocked_sessions = set()
            offset = 0
            while True:
                batch = self.store.list(
                    self.workspace_key, oldest_first=True, limit=100, offset=offset
                )
                for turn in batch:
                    if turn["team_session_id"] in blocked_sessions:
                        continue
                    try:
                        view = await self.get(turn["id"])
                        if view["status"] in {
                            "completed",
                            "error",
                            "cancelled",
                            "unknown",
                            "blocked",
                        }:
                            if any(p["collaboration"]["status"] in _LIVE for p in view["phases"]):
                                blocked_sessions.add(turn["team_session_id"])
                            if turn["cancel_requested"]:
                                for phase in view["phases"]:
                                    await self.collaborations.cancel(phase["collaboration_id"])
                            continue
                        await self._advance(turn, view)
                        updated = await self.get(turn["id"])
                        if updated["status"] not in {
                            "completed",
                            "error",
                            "cancelled",
                            "unknown",
                            "blocked",
                        }:
                            blocked_sessions.add(turn["team_session_id"])
                    except Exception as exc:
                        logger.exception("Team turn advance failed: %s", type(exc).__name__)
                        blocked_sessions.add(turn["team_session_id"])
                        self.record(
                            turn,
                            "recovery-error:" + type(exc).__name__,
                            "recovery_error",
                            {
                                "error_type": type(exc).__name__,
                                "message": "团队恢复遇到错误，请检查执行记录",
                            },
                        )
                        if isinstance(exc, PersonalAgentError) and exc.code in {
                            "budget_exhausted",
                            "version_conflict",
                            "team_scope_denied",
                            "binding_conflict",
                            "session_conflict",
                            "not_found",
                        }:
                            self.record(
                                turn,
                                "execution-rejected",
                                "execution_rejected",
                                {"code": exc.code, "message": exc.message},
                            )
                if len(batch) < 100:
                    break
                offset += len(batch)

    async def _advance(self, turn: dict, view: dict) -> None:
        self.require_live(turn)
        await self._append_request(turn)
        by_phase = {p["phase"]: p["collaboration"] for p in view["phases"]}
        if "planning" not in by_phase:
            await self._create_phase(turn, "planning", [])
            return
        if view["status"] == "delivery_pending":
            await self._deliver(turn, view)
            return
        if by_phase["planning"]["status"] != "completed":
            return
        proposal = self.event(turn, "plan-proposed")
        if proposal is None:
            return
        if "members" not in by_phase:
            await self._create_phase(turn, "members", proposal["data"]["steps"])
        elif by_phase["members"]["status"] == "completed" and "summary" not in by_phase:
            await self._create_phase(turn, "summary", [])

    async def _history(self, turn: dict) -> list[dict]:
        earlier, offset = [], 0
        while True:
            batch = self.store.list(
                self.workspace_key,
                team_session_id=turn["team_session_id"],
                oldest_first=True,
                offset=offset,
                limit=100,
            )
            for previous in batch:
                if previous["id"] == turn["id"]:
                    return earlier[-12:]
                result = await self.get(previous["id"])
                if result["status"] == "completed":
                    earlier.extend(
                        [
                            {
                                "role": "user",
                                "content": previous["client_payload"]["content"][:2000],
                            },
                            {"role": "assistant", "content": result["response"][:2000]},
                        ]
                    )
            if len(batch) < 100:
                return earlier[-12:]
            offset += len(batch)

    async def _create_phase(self, turn: dict, phase: str, proposed_steps: list[dict]) -> None:
        self.require_live(turn)
        used = await self.usage(await self.phases(turn))
        self._require_token_usage(turn, used)
        prepared = self.event(turn, "phase:" + phase)
        if prepared is None:
            team = self.profile(turn["team_id"], turn["team_version"])
            scopes = {item["agent_id"]: item["tool_names"] for item in turn["roster"]["agents"]}
            if phase == "members":
                steps = [
                    {**step, "tool_names": scopes[step["agent_id"]]} for step in proposed_steps
                ]
            else:
                names = sorted(set(team.tool_names) - {"personal_message"})
                if phase == "summary":
                    names = [name for name in names if name != "personal_team"]
                steps = [
                    {
                        "id": phase,
                        "agent_id": team.id,
                        "agent_version": team.version,
                        "task": turn["client_payload"]["content"]
                        if phase == "planning"
                        else (
                            "根据确切成员执行结果回答原始委托；保留来源和不确定性，不虚构成员完成。"
                        ),
                        "depends_on": [],
                        "tool_names": names,
                    }
                ]
            # Missing token deltas are irrelevant only when both dimensions are
            # unbounded; never issue a finite allowance from unknown counters.
            spent = {
                key: used[key] if used[key] is not None else 0
                for key in ("total_cost_usd", "input_tokens", "output_tokens")
            }
            budget = phase_budget(
                turn["policy"], spent, len(steps) + 1 if phase == "members" else 1
            )
            if any(value == 0 for value in budget.values() if value is not None):
                raise PersonalAgentError(
                    "budget_exhausted", "团队任务预算已用完，请查看原结果后调整预算"
                )
            meta = {
                "turn_id": turn["id"],
                "phase": phase,
                "team_id": team.id,
                "team_version": team.version,
                "max_turns": turn["policy"]["max_turns"],
                **budget,
            }
            request = {
                "source_session_id": turn["team_session_id"],
                "request_id": self.phase_request(turn, phase),
                "title": team.name
                + " · "
                + {"planning": "任务规划", "members": "成员执行", "summary": "结果汇总"}[phase],
                "context": "",
                "steps": steps,
                "team_execution": meta,
                "peer_agents": turn["roster"]["agents"] if phase == "members" else [],
            }
            context = {"private_history": await self._history(turn)} if phase == "planning" else {}
            try:
                prepared = self.record(
                    turn,
                    "phase:" + phase,
                    "phase_prepared",
                    {"request": request, "context": context},
                )
            except PersonalAgentError as exc:
                if exc.code != "idempotency_conflict":
                    raise
                prepared = self.event(turn, "phase:" + phase)
        request = dict(prepared["data"]["request"])
        owner = request.pop("team_execution")
        plan = await self.collaborations.create(**request, _team_execution=owner)
        self.record(
            turn,
            "phase-bound:" + phase,
            "phase_bound",
            {
                "phase": phase,
                "collaboration_id": plan["id"],
                "request_sha256": _sha(prepared["data"]["request"]),
            },
        )

    async def execution_context(self, plan: dict, step: dict, shared: str) -> str:
        meta = plan["team_execution"]
        turn = self.store.get(meta["turn_id"], self.workspace_key)
        self._validate_binding(turn, plan, meta["phase"])
        explicit = {
            "content": turn["client_payload"]["content"],
            "context": turn["client_payload"]["context"],
            "sources": turn["client_payload"].get("source_context", ""),
        }
        if meta["phase"] == "members":
            proposal = self.event(turn, "plan-proposed")
            return (
                shared
                + "\n"
                + _json(
                    {
                        "team_turn_id": turn["id"],
                        "explicit": explicit,
                        "shared_context": proposal["data"]["context"],
                    }
                )
            )
        payload = {"team_turn_id": turn["id"], "phase": meta["phase"], "explicit": explicit}
        if meta["phase"] == "planning":
            payload["private_team_history"] = self.event(turn, "phase:planning")["data"]["context"][
                "private_history"
            ]
        else:
            member_phase = next(p for p in await self.phases(turn) if p["phase"] == "members")
            payload["member_results"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "id",
                        "agent_id",
                        "agent_version",
                        "agent_name",
                        "job_id",
                        "run_id",
                        "response",
                        "result_sha256",
                    }
                }
                for item in member_phase["collaboration"]["steps"]
            ]
        return shared + "\n" + _json(payload)

    def bind_execution(self, engine: Any, plan: dict, step: dict) -> None:
        meta = plan["team_execution"]
        turn = self.store.get(meta["turn_id"], self.workspace_key)
        self._validate_binding(turn, plan, meta["phase"])
        engine._personal_team_context = TeamExecution(
            self, turn["id"], meta["phase"], engine, plan["id"], step["task_id"]
        )

    async def _deliver(self, turn: dict, view: dict) -> None:
        self.require_live(turn)
        final_phase = view["phases"][-1]
        step = final_phase["collaboration"]["steps"][-1]
        metadata = {
            **self._metadata(turn),
            "collaboration_id": final_phase["collaboration_id"],
            "run_id": step["run_id"],
            "result_sha256": step["result_sha256"],
        }
        try:
            await self.engine.session_store.append_personal_messages_once(
                turn["team_session_id"],
                expected_agent_id=turn["team_id"],
                delivery_id="result-" + turn["id"],
                messages=[
                    {
                        "id": "team-result-" + turn["id"],
                        "role": "assistant",
                        "content": view["response"],
                        "metadata": metadata,
                    }
                ],
                usage_tokens=view["usage"]["total_tokens"],
                usage_cost_usd=view["usage"]["total_cost_usd"],
            )
        except ValueError as exc:
            raise PersonalAgentError("session_conflict", str(exc)) from exc
        self.record(
            turn,
            "delivered",
            "result_delivered",
            {
                "message_id": "team-result-" + turn["id"],
                "session_id": turn["team_session_id"],
                "collaboration_id": final_phase["collaboration_id"],
                "job_id": step["job_id"],
                "run_id": step["run_id"],
                "result_sha256": step["result_sha256"],
                "response_sha256": hashlib.sha256(view["response"].encode("utf-8")).hexdigest(),
            },
        )

    async def dispatch_from_engine(self, task: str, context: str = ""):
        from naumi_agent.orchestrator.engine import AgentResult

        session = self.engine._session
        team = self.personal.current(session.id)
        result = await self.start_turn(
            source_session_id=session.id,
            team_id=team.id,
            team_version=team.version,
            request_id="chat-" + uuid.uuid4().hex,
            content=task,
            context=context,
        )
        return AgentResult(
            status="admitted",
            response=(
                f"团队任务已受理：{result['id']}。执行独立于当前对话，"
                f"可通过 /assistants team-turn {result['id']} 查看结果。"
            ),
        )


class PersonalTeamTool(Tool):
    name = "personal_team"
    description = (
        "只在当前团队规划阶段使用。members分页查看已授权的实际成员职责和工具。"
        "propose提交1至12步真实成员DAG；每步绑定agent_id、agent_version、task、depends_on。"
        "只共享明确context，不转发团队私有历史。提交后结束当前规划回答，后台会在释放本Job容量后执行成员并汇总。"
        "计划已保存不表示成员已执行；不要同步等待、轮询或声称完成。"
    )

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def metadata(self):
        return ToolMetadata(
            read_only=False,
            concurrency_safe=False,
            user_facing_name="团队协调",
            search_hint="personal persistent team plan members delegate",
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["members", "propose"]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "context": {"type": "string", "maxLength": 32000},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "agent_id", "agent_version", "task", "depends_on"],
                        "properties": {
                            "id": {"type": "string"},
                            "agent_id": {"type": "string"},
                            "agent_version": {"type": "integer", "minimum": 1},
                            "task": {"type": "string", "minLength": 1, "maxLength": 16000},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        }

    async def execute(self, **kwargs):
        try:
            if not Draft202012Validator(self.parameters_schema).is_valid(kwargs):
                raise PersonalAgentError("invalid_input", "团队协调参数不正确")
            context = getattr(self.engine, "_personal_team_context", None)
            if context is None or context.phase != "planning":
                raise PersonalAgentError("team_scope_denied", "请在当前团队的真实规划阶段提交分工")
            await context.authorize()
            service = context.service
            turn = service.store.get(context.turn_id, service.workspace_key)
            if kwargs["operation"] == "members":
                if set(kwargs) - {"operation", "offset", "limit"}:
                    raise PersonalAgentError("invalid_input", "members仅接受offset与limit分页参数")
                entries = []
                for grant in turn["roster"]["agents"]:
                    profile = service.profile(grant["agent_id"], grant["agent_version"])
                    entries.append(
                        {
                            **grant,
                            "name": profile.name,
                            "description": profile.description,
                            "instructions": profile.instructions[:1200],
                            "instructions_truncated": len(profile.instructions) > 1200,
                        }
                    )
                offset = kwargs.get("offset", 0)
                result = {
                    "members": [],
                    "total": len(entries),
                    "offset": offset,
                    "next_offset": None,
                }
                for entry in entries[offset : offset + kwargs.get("limit", 20)]:
                    candidate = {**result, "members": [*result["members"], entry]}
                    if len(_json(candidate)) >= 10900:
                        break
                    result["members"].append(entry)
                end = offset + len(result["members"])
                result["next_offset"] = end if end < len(entries) else None
            else:
                if set(kwargs) - {"operation", "steps", "context"} or "steps" not in kwargs:
                    raise PersonalAgentError(
                        "invalid_input", "propose需要steps，可附context，不接受其他字段"
                    )
                team = service.profile(turn["team_id"], turn["team_version"])
                if "personal_team" not in team.tool_names:
                    raise PersonalAgentError("team_scope_denied", "当前团队未启用成员任务协调")
                records = await self.engine.chat_run_store.list_runs(
                    self.engine._session.id, limit=1
                )
                if not records or records[0].status != "running":
                    raise PersonalAgentError("binding_conflict", "团队规划缺少真实运行记录")
                data = {
                    "steps": normalize_plan(kwargs["steps"], turn["roster"]),
                    "context": text(kwargs.get("context", ""), "共享资料", 32000, required=False),
                    "source_run_id": records[0].id,
                    "collaboration_id": context.plan_id,
                    "task_id": context.task_id,
                }
                service.record(turn, "plan-proposed", "plan_proposed", data)
                result = {
                    "status": "plan_saved",
                    "turn_id": turn["id"],
                    "plan_sha256": _sha(data),
                    "step_ids": [s["id"] for s in data["steps"]],
                }
            return _json(result)
        except PersonalAgentError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc
