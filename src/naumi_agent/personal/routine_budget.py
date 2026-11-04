"""Persistent routine reservations and fail-closed, per-transport estimate permits."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import asdict, is_dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from naumi_agent.model.router import ModelTier
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import SHA, canonical, digest, integer, text

ESTIMATE_POLICY = "routine-transport-estimate-v1"
_PRICE_SOURCES = frozenset({"catalog", "config", "litellm"})
_PRESTART_REASONS = frozenset(
    {
        "agent_job_cancelled_before_claim",
        "agent_job_cancelled_before_start",
    }
)


def _error(code, message):
    return PersonalAgentError(code, message)


def _definition(routine):
    return routine.get("definition", routine)


def _key(routine):
    return text(routine.get("id"), "周期编号", 2300)


def _reservation_id(occurrence_id):
    return "reservation:" + text(occurrence_id, "发生编号", 2300)


def _evidence(value):
    if not isinstance(value, dict) or len(canonical(value).encode("utf-8")) > 32768:
        raise _error("invalid_input", "预算证据需要有效的有限对象")
    return {"evidence": value, "evidence_sha256": digest(value)}


def _head(tx, routine_id):
    value = tx.get("budget", "budget:" + routine_id)
    if value.get("routine_id") != routine_id:
        raise _error("storage_error", "预算原件与周期绑定不一致")
    for field in (
        "reserved_cost_microusd",
        "settled_cost_microusd",
        "reserved_executions",
        "started_executions",
        "unknown_reservations",
    ):
        integer(value.get(field), "持久预算计数", minimum=0)
    return value


def _reservation(tx, occurrence):
    occurrence_id = text(occurrence.get("id"), "发生编号", 2300)
    value = tx.get("budget", _reservation_id(occurrence_id))
    if value.get("occurrence_id") != occurrence_id or value.get("routine_id") != occurrence.get(
        "routine_id"
    ):
        raise _error("binding_conflict", "发生记录与原预算预留不一致")
    return value


def _put(tx, kind, original, **changes):
    return tx.put(kind, {**original, **changes}, expected_revision=original["revision"])


def budget_initialize(tx, routine):
    identity = _key(routine)
    previous = tx.get("budget", "budget:" + identity, missing=True)
    if previous is not None:
        return _head(tx, identity)
    return tx.put(
        "budget",
        {
            "id": "budget:" + identity,
            "routine_id": identity,
            "status": "active",
            "reserved_cost_microusd": 0,
            "settled_cost_microusd": 0,
            "reserved_executions": 0,
            "started_executions": 0,
            "unknown_reservations": 0,
            "estimate_policy": ESTIMATE_POLICY,
        },
        expected_revision=0,
    )


def budget_view(tx, routine):
    head = _head(tx, _key(routine))
    limits = _definition(routine)["limits"]
    if (
        limits["total_cost_microusd"]
        < head["settled_cost_microusd"] + head["reserved_cost_microusd"]
        or limits["max_executions"] < head["started_executions"] + head["reserved_executions"]
    ):
        raise _error("budget_exhausted", "新限额低于原执行和未结预留，请先核对原预算")
    return {
        **head,
        "total_cost_microusd": limits["total_cost_microusd"],
        "max_executions": limits["max_executions"],
        "remaining_cost_microusd": max(
            0,
            limits["total_cost_microusd"]
            - head["settled_cost_microusd"]
            - head["reserved_cost_microusd"],
        ),
        "remaining_executions": max(
            0, limits["max_executions"] - head["started_executions"] - head["reserved_executions"]
        ),
        "usage_complete": head["unknown_reservations"] == 0,
    }


def budget_reserve(tx, routine, occurrence_id):
    identity = _key(routine)
    reservation_id = _reservation_id(occurrence_id)
    prior = tx.get("budget", reservation_id, missing=True)
    if prior is not None:
        if prior.get("routine_id") != identity or prior.get("occurrence_id") != occurrence_id:
            raise _error("binding_conflict", "原发生预算绑定不一致")
        return prior
    view = budget_view(tx, routine)
    if view["status"] != "active" or view["remaining_executions"] == 0:
        raise _error("budget_exhausted", "周期执行次数已用完，请核对原执行后调整限额")
    per_run = _definition(routine)["policy"]["max_cost_microusd"]
    integer(per_run, "单次估算费用上限", minimum=0)
    amount = min(per_run, view["remaining_cost_microusd"])
    if amount == 0:
        raise _error("budget_exhausted", "周期估算费用额度已用完，未启动新的模型请求")
    _put(
        tx,
        "budget",
        _head(tx, identity),
        reserved_cost_microusd=view["reserved_cost_microusd"] + amount,
        reserved_executions=view["reserved_executions"] + 1,
    )
    return tx.put(
        "budget",
        {
            "id": reservation_id,
            "routine_id": identity,
            "occurrence_id": occurrence_id,
            "status": "reserved",
            "cost_microusd": amount,
            "started": False,
            "settlement": "reserved",
            "known_cost_microusd": None,
            "evidence": {},
            "evidence_sha256": digest({}),
            "transport_reserved_microusd": 0,
            "transport_settled_microusd": 0,
            "pending_calls": 0,
            "unknown_calls": 0,
            "next_call_sequence": 1,
        },
        expected_revision=0,
    )


def budget_started(tx, occurrence):
    reservation = _reservation(tx, occurrence)
    if reservation["started"]:
        return reservation
    if reservation["settlement"] in {"known", "prestart_cancelled"}:
        raise _error("binding_conflict", "已结算的原预留未重新开始执行")
    head = _head(tx, reservation["routine_id"])
    if head["reserved_executions"] <= 0:
        raise _error("storage_error", "原执行次数预留缺失")
    _put(
        tx,
        "budget",
        head,
        reserved_executions=head["reserved_executions"] - 1,
        started_executions=head["started_executions"] + 1,
    )
    return _put(tx, "budget", reservation, started=True)


def _prestart_proof(tx, occurrence, evidence):
    if evidence.get("proof_kind") == "routine_pre_dispatch":
        identity = occurrence["id"]
        original = tx.get("occurrence", identity)
        decision_sha = evidence.get("decision_sha256")
        if (
            evidence.get("occurrence_id") != identity
            or original.get("routine_id") != occurrence.get("routine_id")
            or original.get("item_create_attempted") is not False
            or original.get("item_id") is not None
            or original.get("status") not in {"skipped", "blocked"}
            or not isinstance(decision_sha, str)
            or SHA.fullmatch(decision_sha) is None
            or not isinstance(original.get("decision"), dict)
            or original.get("decision_digest") != decision_sha
            or digest(original["decision"]) != decision_sha
        ):
            return False
        for revision in range(1, original["revision"]):
            previous = tx.get("occurrence", identity, revision=revision)
            if (
                previous.get("item_create_attempted") is not False
                or previous.get("item_id") is not None
            ):
                return False
        return True
    receipt = evidence.get("lifecycle_receipt", evidence.get("receipt"))
    if not isinstance(receipt, dict):
        return False
    return (
        receipt.get("reason_code") in _PRESTART_REASONS
        and isinstance(receipt.get("receipt_sha256"), str)
        and SHA.fullmatch(receipt["receipt_sha256"]) is not None
        and isinstance(evidence.get("job_id"), str)
        and bool(evidence["job_id"])
        and isinstance(evidence.get("request_sha256"), str)
        and SHA.fullmatch(evidence["request_sha256"]) is not None
    )


def budget_settle(tx, occurrence, known_cost_microusd, evidence, started):
    if type(started) is not bool:
        raise _error("invalid_input", "原执行启动状态格式不正确")
    proof = _evidence(evidence)
    reservation = _reservation(tx, occurrence)
    if started and not reservation["started"]:
        reservation = budget_started(tx, occurrence)
    if known_cost_microusd is not None:
        integer(known_cost_microusd, "原执行已知估算费用", minimum=0)
    known = known_cost_microusd
    if known is not None:
        known = max(known, reservation["transport_settled_microusd"])
    if reservation["settlement"] in {"known", "prestart_cancelled"}:
        if known == reservation["known_cost_microusd"]:
            return reservation
        raise _error("binding_conflict", "原执行费用已结算，请核对原费用证据")
    unresolved = reservation["pending_calls"] or reservation["unknown_calls"]
    prestart = (
        not reservation["started"]
        and known == 0
        and not unresolved
        and _prestart_proof(tx, occurrence, evidence)
    )
    if not reservation["started"] and not prestart:
        known = None
    overrun = known is not None and known > reservation["cost_microusd"]
    if unresolved or overrun:
        known = None
    head = _head(tx, reservation["routine_id"])
    already_unknown = reservation["settlement"] == "unknown"
    if known is None:
        if overrun:
            proof = _evidence({**evidence, "reported_overrun_cost_microusd": known_cost_microusd})
        if already_unknown and reservation["evidence_sha256"] == proof["evidence_sha256"]:
            return reservation
        if not already_unknown or overrun:
            _put(
                tx,
                "budget",
                head,
                unknown_reservations=head["unknown_reservations"] + (not already_unknown),
                status="blocked" if overrun else head["status"],
            )
        return _put(tx, "budget", reservation, status="unknown", settlement="unknown", **proof)
    _put(
        tx,
        "budget",
        head,
        reserved_cost_microusd=head["reserved_cost_microusd"] - reservation["cost_microusd"],
        settled_cost_microusd=head["settled_cost_microusd"] + known,
        reserved_executions=head["reserved_executions"] - (1 if prestart else 0),
        unknown_reservations=head["unknown_reservations"] - (1 if already_unknown else 0),
    )
    return _put(
        tx,
        "budget",
        reservation,
        status="settled",
        known_cost_microusd=known,
        settlement="prestart_cancelled" if prestart else "known",
        **proof,
    )


def _decimal(value, label):
    if isinstance(value, bool):
        raise _error("usage_unknown", f"{label}缺少有效证据")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError()
        return number
    except (ValueError, InvalidOperation, TypeError):
        raise _error("usage_unknown", f"{label}缺少有效证据") from None


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _price_and_input(router, args, kwargs):
    if len(args) > 1 or args and "messages" in kwargs:
        raise _error("invalid_input", "模型请求参数重复或格式不正确")
    messages = args[0] if args else kwargs.get("messages")
    if not isinstance(messages, list):
        raise _error("invalid_input", "模型请求缺少确切消息")
    for message in messages:
        if not isinstance(message, dict):
            raise _error("invalid_input", "模型消息格式不正确")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise _error("budget_unpriced", "本次多模态输入缺少可核验费用上界，未发出请求")
    model = kwargs.get("model") or router.resolve_model(kwargs.get("tier", ModelTier.CAPABLE))
    contract = router.get_model_capability_contract(model)
    sources = {
        field: contract.field_sources.get(field, "unavailable")
        for field in ("input_cost_per_million", "output_cost_per_million")
    }
    if any(source not in _PRICE_SOURCES for source in sources.values()):
        raise _error("budget_unpriced", "模型单价尚未核验，未使用缺失或备用单价启动请求")
    input_rate = _decimal(contract.input_cost_per_million, "模型输入单价")
    output_rate = _decimal(contract.output_cost_per_million, "模型输出单价")
    tools = kwargs.get("tools") or []
    raw = canonical(
        {"messages": messages, "tools": tools, "response_format": kwargs.get("response_format")}
    )
    size = len(raw.encode("utf-8"))
    if size > 8 * 1024**2:
        raise _error("invalid_input", "模型请求输入超过预算预留容量上限")
    # This is an explicit local estimate bound, not a provider invoice authority.
    upper = size + 4096 + 128 * (len(messages) + len(tools))
    requested = kwargs.get("max_tokens")
    if requested is None:
        requested = router.get_max_output(model)
    integer(requested, "模型输出上限", maximum=10**7)
    ceiling = router.get_max_output(model)
    integer(ceiling, "模型有限输出上限", maximum=10**7)
    pricing = {
        "model": model,
        "input_per_million": str(input_rate),
        "output_per_million": str(output_rate),
        "sources": sources,
        "estimate_policy": ESTIMATE_POLICY,
    }
    return (
        pricing,
        upper,
        min(requested, ceiling),
        hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        size,
    )


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


class RoutineBudgetMixin:
    """Transport hooks for the parent service; no parallel model/runtime path."""

    budget_initialize = staticmethod(budget_initialize)
    budget_reserve = staticmethod(budget_reserve)
    budget_view = staticmethod(budget_view)
    budget_started = staticmethod(budget_started)
    budget_settle = staticmethod(budget_settle)

    async def reserve_transport_call(self, intent, router, args, kwargs):
        occurrence = await _maybe(self.occurrence_for_item(intent["item_id"]))
        if occurrence is None:
            return None
        await _maybe(self.authorize_occurrence_execution(occurrence, intent))
        pricing, upper, requested, input_sha, input_bytes = _price_and_input(router, args, kwargs)

        def reserve(tx):
            reservation = _reservation(tx, occurrence)
            if _head(tx, reservation["routine_id"])["status"] != "active":
                raise _error("budget_exhausted", "原预算存在超额证据，已停止新的模型请求")
            if reservation["settlement"] not in {"reserved", "unknown"}:
                raise _error("execution_unknown", "原执行预算已结束，未发出新的模型请求")
            if reservation["pending_calls"] or reservation["unknown_calls"]:
                raise _error("usage_unknown", "原模型请求用量待核对，未自动重复请求")
            if reservation["settlement"] == "unknown":
                raise _error("usage_unknown", "原发生费用待核对，未恢复模型执行")
            available = reservation["cost_microusd"] - reservation["transport_settled_microusd"]
            input_cost = Decimal(pricing["input_per_million"]) * upper
            output_rate = Decimal(pricing["output_per_million"])
            affordable = (
                requested
                if output_rate == 0
                else int(
                    ((Decimal(available) - input_cost) / output_rate).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
            )
            output = min(requested, affordable)
            amount = _ceil(input_cost + output_rate * output) if output > 0 else 0
            if available <= 0 or output <= 0 or amount > available:
                raise _error("budget_exhausted", "本次模型请求超过剩余额度，未发出请求")
            if not reservation["started"]:
                reservation = budget_started(tx, occurrence)
            sequence = reservation["next_call_sequence"]
            call_id = "call:" + occurrence["id"] + ":" + f"{sequence:012d}"
            permit = tx.put(
                "call",
                {
                    "id": call_id,
                    "call_id": call_id,
                    "routine_id": reservation["routine_id"],
                    "occurrence_id": occurrence["id"],
                    "reservation_id": reservation["id"],
                    "status": "sent",
                    "sequence": sequence,
                    "max_tokens": output,
                    "reserved_cost_microusd": amount,
                    "known_cost_microusd": None,
                    "input_upper_tokens": upper,
                    "input_bytes": input_bytes,
                    "input_sha256": input_sha,
                    "pricing": pricing,
                    "request_sha256": intent["request"]["request_sha256"],
                    "item_id": intent["item_id"],
                    "evidence": {},
                    "evidence_sha256": digest({}),
                },
                expected_revision=0,
            )
            _put(
                tx,
                "budget",
                reservation,
                transport_reserved_microusd=reservation["transport_reserved_microusd"] + amount,
                pending_calls=reservation["pending_calls"] + 1,
                next_call_sequence=sequence + 1,
            )
            return permit

        return await _maybe(self._write(reserve))

    async def complete_transport_call(self, permit, usage, evidence):
        if permit is None:
            return None
        proof = _evidence(evidence)
        values = asdict(usage) if is_dataclass(usage) else usage
        if not isinstance(values, dict):
            await self.unknown_transport_call(permit, {"reason": "missing_usage"})
            raise _error("usage_unknown", "模型请求缺少确切用量，预算保持未知")
        try:
            inp = integer(values.get("input_tokens"), "模型输入用量", minimum=0)
            out = integer(values.get("output_tokens"), "模型输出用量", minimum=0)
            reported = _decimal(values.get("cost_usd"), "模型报告费用")
            if (
                evidence.get("usage_source") != "transport_response"
                or evidence.get("completed") is not True
                or evidence.get("terminal") is not True
                or inp > permit["input_upper_tokens"]
                or out > permit["max_tokens"]
            ):
                raise _error("usage_unknown", "模型用量来源或请求上界尚未得到完整证明")
            price = permit["pricing"]
            amount = max(
                _ceil(reported * 1_000_000),
                _ceil(
                    Decimal(price["input_per_million"]) * inp
                    + Decimal(price["output_per_million"]) * out
                ),
            )
            if amount > permit["reserved_cost_microusd"]:
                await self.unknown_transport_call(
                    permit,
                    {
                        **evidence,
                        "reason": "reservation_overrun",
                        "observed_cost_microusd": amount,
                    },
                )
                raise _error("usage_unknown", "模型报告费用超出原预留，预算保持未知")
        except PersonalAgentError:
            await self.unknown_transport_call(permit, {**evidence, "reason": "usage_unverified"})
            raise

        def complete(tx):
            call = tx.get("call", permit["call_id"])
            self._check_permit(call, permit)
            if call["status"] == "known":
                if call["known_cost_microusd"] != amount:
                    raise _error("binding_conflict", "原模型请求对应不同用量，未改写费用")
                return call
            if call["status"] != "sent":
                raise _error("usage_unknown", "原模型请求费用保持未知，请核对原请求")
            reservation = tx.get("budget", call["reservation_id"])
            _put(
                tx,
                "budget",
                reservation,
                pending_calls=reservation["pending_calls"] - 1,
                transport_reserved_microusd=reservation["transport_reserved_microusd"]
                - call["reserved_cost_microusd"],
                transport_settled_microusd=reservation["transport_settled_microusd"] + amount,
            )
            return _put(tx, "call", call, status="known", known_cost_microusd=amount, **proof)

        return await _maybe(self._write(complete))

    @staticmethod
    def _check_permit(call, permit):
        for key in (
            "call_id",
            "occurrence_id",
            "routine_id",
            "reservation_id",
            "sequence",
            "reserved_cost_microusd",
            "request_sha256",
            "input_sha256",
            "pricing",
            "max_tokens",
            "input_upper_tokens",
            "input_bytes",
            "item_id",
        ):
            if call.get(key) != permit.get(key):
                raise _error("binding_conflict", "模型请求许可与持久原件不一致")

    async def unknown_transport_call(self, permit, evidence):
        if permit is None:
            return None
        proof = _evidence(evidence)

        def unknown(tx):
            call = tx.get("call", permit["call_id"])
            self._check_permit(call, permit)
            if call["status"] in {"unknown", "known"}:
                return call
            reservation = tx.get("budget", call["reservation_id"])
            observed_cost = evidence.get("observed_cost_microusd")
            if type(observed_cost) is int and observed_cost > call["reserved_cost_microusd"]:
                _put(tx, "budget", _head(tx, call["routine_id"]), status="blocked")
            _put(
                tx,
                "budget",
                reservation,
                pending_calls=reservation["pending_calls"] - 1,
                unknown_calls=reservation["unknown_calls"] + 1,
            )
            return _put(tx, "call", call, status="unknown", **proof)

        return await _maybe(self._write(unknown))
