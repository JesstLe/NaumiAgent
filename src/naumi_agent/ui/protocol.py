"""Stable JSONL protocol shared by NaumiAgent terminal frontends.

The protocol is intentionally transport-agnostic.  The first implementation
uses stdin/stdout JSONL so an Ink/pi-tui frontend can run as an independent
process while Python remains the single owner of AgentEngine, tools, memory,
safety, and debug tracing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from naumi_agent.harness.checks import validate_run_id
from naumi_agent.ui.messages.base import UIMessage

PROTOCOL_VERSION = 1
PROTOCOL_MINIMUM_VERSION = 1
PROTOCOL_MAXIMUM_VERSION = 1
PROTOCOL_CAPABILITIES = (
    "goal_snapshot",
    "heartbeat",
    "task_snapshot",
    "typed_ui_messages",
    "workbench_snapshot",
    "workbench_proposal_actions",
)
PROTOCOL_REQUIRED_CAPABILITIES = ("typed_ui_messages",)
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProtocolNegotiationError(ValueError):
    """A typed hello negotiation failure safe to expose to terminal clients."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ClientEventType(StrEnum):
    """Events accepted from a terminal frontend."""

    HELLO = "hello"
    SUBMIT = "submit"
    TASK_SUBMIT = "task_submit"
    RUN_CANCEL = "run_cancel"
    RECEIPT_REQUEST = "receipt/request"
    HARNESS_EXPLAIN_REQUEST = "harness/explain/request"
    HARNESS_REPLAY_REQUEST = "harness/replay/request"
    HARNESS_EVAL_BASELINE_REQUEST = "harness/eval-baseline/request"
    HARNESS_EVAL_BATCH_REQUEST = "harness/eval-batch/request"
    HARNESS_EVAL_PROMOTION_REQUEST = "harness/eval-promotion/request"
    INSPECTOR_REQUEST = "inspector/request"
    AGENTS_REQUEST = "agents/request"
    AGENTS_STOP = "agents/stop"
    WORKBENCH_REQUEST = "workbench/request"
    WORKBENCH_REVIEW_REQUEST = "workbench/review/request"
    WORKBENCH_PROPOSAL_ACTION = "workbench/proposal/action"
    EVOLUTION_REVIEW_REQUEST = "evolution/review/request"
    SET_MODE = "set_mode"
    CYCLE_MODE = "cycle_mode"
    SET_REASONING = "set_reasoning"
    PERMISSION_RESPONSE = "permission_response"
    INTERACTION_RESPONSE = "interaction_response"
    PERMISSION_REVOKE = "permission_revoke"
    RESUME = "resume"
    GOAL_PANEL = "goal_panel"
    TASK_PANEL = "task_panel"
    TASK_CANCEL = "task_cancel"
    PERMISSIONS_PANEL = "permissions_panel"
    DOCTOR = "doctor"
    PING = "ping"
    SHUTDOWN = "shutdown"


_CLIENT_EVENT_NAMES = {str(event) for event in ClientEventType}
_PAYLOAD_LIMIT_DEFAULTS = {
    ClientEventType.GOAL_PANEL: 20,
    ClientEventType.TASK_PANEL: 12,
    ClientEventType.PERMISSIONS_PANEL: 12,
}


class ServerEventType(StrEnum):
    """Events emitted to a terminal frontend."""

    READY = "ready"
    ACK = "ack"
    ERROR = "error"
    PONG = "pong"
    USER_MESSAGE = "user/message"
    TASK_CREATED = "task/created"
    UI_MESSAGE = "ui/message"
    ENGINE_EVENT = "engine/event"
    COMPLETION_RECEIPT = "completion/receipt"
    HARNESS_RECEIPT = "harness/receipt"
    HARNESS_EXPLAIN = "harness/explain"
    HARNESS_REPLAY = "harness/replay"
    HARNESS_EVAL_BASELINE = "harness/eval-baseline"
    HARNESS_EVAL_BATCH = "harness/eval-batch"
    HARNESS_EVAL_PROMOTION = "harness/eval-promotion"
    DOCTOR_HEALTH = "doctor/health"
    INSPECTOR_SNAPSHOT = "inspector/snapshot"
    INSPECTOR_UPDATE = "inspector/update"
    AGENTS_SNAPSHOT = "agents/snapshot"
    AGENTS_UPDATE = "agents/update"
    AGENTS_ACTION = "agents/action"
    RUN_QUEUED = "run/queued"
    RUN_STARTED = "run/started"
    RUN_COMPLETED = "run/completed"
    RUN_CANCELLED = "run/cancelled"
    SESSION_REPLAYED = "session/replayed"
    STATUS = "runtime/status"
    MODE_CHANGED = "mode/changed"
    PERMISSION_REQUEST = "permission/request"
    PERMISSION_RESOLVED = "permission/resolved"
    INTERACTION_REQUEST = "interaction/request"
    INTERACTION_RESOLVED = "interaction/resolved"
    PERMISSION_GRANTS_CHANGED = "permission/grants_changed"
    PERMISSION_SNAPSHOT = "permissions/snapshot"
    GOALS_SNAPSHOT = "goals/snapshot"
    TASKS_SNAPSHOT = "tasks/snapshot"
    DEBUG_TRACE = "debug/trace"
    WORKBENCH_SNAPSHOT = "workbench/snapshot"
    WORKBENCH_EVENT = "workbench/event"
    WORKBENCH_REVIEW = "workbench/review"
    WORKBENCH_PROPOSAL_ACTION_RESULT = "workbench/proposal/action_result"
    EVOLUTION_REVIEW = "evolution/review"
    SHUTDOWN = "shutdown"


def make_envelope(
    event: ServerEventType | str,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any]:
    """Build one protocol envelope."""
    record: dict[str, Any] = {
        "type": str(event),
        "version": PROTOCOL_VERSION,
        "id": uuid4().hex[:12],
        "ts": datetime.now().isoformat(),
        "payload": payload or {},
    }
    if request_id:
        record["request_id"] = request_id
    if sequence is not None:
        record["seq"] = sequence
    return record


def ui_message_payload(message: UIMessage) -> dict[str, Any]:
    """Serialize a typed UIMessage into a JSON-safe payload."""
    data = asdict(message) if is_dataclass(message) else dict(message)  # type: ignore[arg-type]
    data["type"] = str(data.get("type", ""))
    return data


def encode_jsonl(record: dict[str, Any]) -> str:
    """Serialize a protocol record as strict LF-framed JSONL."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_jsonl_line(line: str) -> dict[str, Any]:
    """Decode and validate a single client JSONL line."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONL 解析失败: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("JSONL 记录必须是对象。")
    if "type" not in value:
        raise ValueError("JSONL 记录缺少 type 字段。")
    return value


def normalize_client_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate one client event before bridge dispatch."""
    if not isinstance(record, dict):
        raise ValueError("客户端事件必须是对象。")

    event_type = str(record.get("type") or "")
    if not event_type:
        raise ValueError("客户端事件缺少 type 字段。")
    if event_type not in _CLIENT_EVENT_NAMES:
        raise ValueError(f"未知客户端事件: {event_type}")

    version = record.get("version")
    if version is not None:
        try:
            parsed_version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValueError("协议 version 必须是整数。") from exc
        if parsed_version != PROTOCOL_VERSION:
            raise ValueError(
                f"协议 version 不兼容: {parsed_version}，当前支持 {PROTOCOL_VERSION}。"
            )

    payload = record.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象。")

    normalized = dict(record)
    normalized["type"] = event_type
    normalized["version"] = PROTOCOL_VERSION
    if "id" in normalized and normalized["id"] is not None:
        normalized["id"] = str(normalized["id"])
    if "request_id" in normalized and normalized["request_id"] is not None:
        normalized["request_id"] = str(normalized["request_id"])
    normalized["payload"] = _normalize_client_payload(ClientEventType(event_type), payload)
    return normalized


def _normalize_client_payload(
    event_type: ClientEventType,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type == ClientEventType.HELLO:
        return _normalize_hello_payload(payload)

    if event_type == ClientEventType.SUBMIT:
        return {"text": str(payload.get("text") or "")}

    if event_type == ClientEventType.TASK_SUBMIT:
        text = str(payload.get("text") or "")
        if not text.strip():
            raise ValueError("任务内容不能为空。")
        if len(text) > 200_000:
            raise ValueError("任务内容不能超过 200000 个字符。")
        title = str(payload.get("title") or "").strip()
        if len(title) > 200:
            raise ValueError("任务标题不能超过 200 个字符。")
        parallel_mode = str(payload.get("parallel_mode") or "exclusive").strip().lower()
        if parallel_mode not in {"exclusive", "cooperative", "competitive", "exploratory"}:
            raise ValueError(
                "并行模式无效，可用值: "
                "exclusive / cooperative / competitive / exploratory。"
            )
        risk_level = str(payload.get("risk_level") or "medium").strip().lower()
        if risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("风险等级无效，可用值: low / medium / high / critical。")
        return {
            "text": text,
            "mission_id": str(payload.get("mission_id") or "").strip(),
            "title": title,
            "acceptance_criteria": _normalize_text_list(
                payload.get("acceptance_criteria"),
                field="acceptance_criteria",
                max_items=20,
                max_chars=500,
            ),
            "blocked_by": _normalize_text_list(
                payload.get("blocked_by"),
                field="blocked_by",
                max_items=50,
                max_chars=128,
            ),
            "parallel_mode": parallel_mode,
            "risk_level": risk_level,
        }

    if event_type == ClientEventType.RUN_CANCEL:
        reason = str(payload.get("reason") or "").strip()
        if len(reason) > 500:
            raise ValueError("取消原因不能超过 500 个字符。")
        return {"reason": reason}

    if event_type == ClientEventType.RECEIPT_REQUEST:
        receipt_id = str(payload.get("receipt_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        if not receipt_id and not run_id:
            raise ValueError("回执补发请求缺少 receipt_id 或 run_id。")
        return {
            "session_id": str(payload.get("session_id") or "").strip(),
            "receipt_id": receipt_id,
            "run_id": run_id,
        }

    if event_type in {
        ClientEventType.HARNESS_EXPLAIN_REQUEST,
        ClientEventType.HARNESS_REPLAY_REQUEST,
    }:
        return _normalize_harness_detail_request(payload)

    if event_type == ClientEventType.HARNESS_EVAL_BASELINE_REQUEST:
        suite_id = str(payload.get("suite_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite_id):
            raise ValueError(
                "Harness Eval suite_id 必须以小写字母开头，且只含小写字母、数字、_ 或 -。"
            )
        return {"suite_id": suite_id}

    if event_type == ClientEventType.HARNESS_EVAL_BATCH_REQUEST:
        suite_id = str(payload.get("suite_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite_id):
            raise ValueError("Harness Eval Batch suite_id 格式无效。")
        repetitions = payload.get("repetitions", 5)
        if isinstance(repetitions, bool) or not isinstance(repetitions, int):
            raise ValueError("Harness Eval Batch repetitions 必须是整数。")
        if not 5 <= repetitions <= 100:
            raise ValueError("Harness Eval Batch repetitions 必须在 5..100 之间。")
        batch_id = str(payload.get("batch_id") or "").strip()
        if batch_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch_id):
            raise ValueError("Harness Eval Batch batch_id 格式无效。")
        return {
            "suite_id": suite_id,
            "repetitions": repetitions,
            "batch_id": batch_id,
        }

    if event_type == ClientEventType.HARNESS_EVAL_PROMOTION_REQUEST:
        suite_id = str(payload.get("suite_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite_id):
            raise ValueError("Harness Eval Promotion suite_id 格式无效。")
        batch_id = str(payload.get("batch_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch_id):
            raise ValueError("Harness Eval Promotion batch_id 格式无效。")
        reason = str(payload.get("reason") or "").strip()
        if reason and not 3 <= len(reason) <= 2_000:
            raise ValueError("Harness Eval Promotion reason 必须是 3..2000 个字符。")
        return {"suite_id": suite_id, "batch_id": batch_id, "reason": reason}

    if event_type == ClientEventType.INSPECTOR_REQUEST:
        raw_revision = payload.get("known_revision", 0)
        if isinstance(raw_revision, bool):
            raise ValueError("Inspector known_revision 必须是非负整数。")
        try:
            known_revision = int(raw_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("Inspector known_revision 必须是非负整数。") from exc
        if known_revision < 0 or known_revision > 2_147_483_647:
            raise ValueError("Inspector known_revision 必须是非负整数。")
        session_id = str(payload.get("session_id") or "").strip()
        if len(session_id) > 500:
            raise ValueError("Inspector session_id 不能超过 500 个字符。")
        return {
            "open": _to_bool(payload.get("open", True)),
            "known_revision": known_revision,
            "session_id": session_id,
        }

    if event_type == ClientEventType.AGENTS_REQUEST:
        raw_revision = payload.get("known_revision", 0)
        if isinstance(raw_revision, bool):
            raise ValueError("Agent known_revision 必须是非负整数。")
        try:
            known_revision = int(raw_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent known_revision 必须是非负整数。") from exc
        if known_revision < 0 or known_revision > 2_147_483_647:
            raise ValueError("Agent known_revision 必须是非负整数。")
        session_id = str(payload.get("session_id") or "").strip()
        if len(session_id) > 500:
            raise ValueError("Agent session_id 不能超过 500 个字符。")
        return {
            "open": _to_bool(payload.get("open", True)),
            "known_revision": known_revision,
            "session_id": session_id,
        }

    if event_type == ClientEventType.WORKBENCH_REQUEST:
        raw_revision = payload.get("known_revision", 0)
        if isinstance(raw_revision, bool):
            raise ValueError("Workbench known_revision 必须是非负整数。")
        try:
            known_revision = int(raw_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("Workbench known_revision 必须是非负整数。") from exc
        if known_revision < 0 or known_revision > 2_147_483_647:
            raise ValueError("Workbench known_revision 必须是非负整数。")
        session_id = str(payload.get("session_id") or "").strip()
        stream_id = str(payload.get("known_stream_id") or "").strip()
        if len(session_id) > 500:
            raise ValueError("Workbench session_id 不能超过 500 个字符。")
        if len(stream_id) > 128:
            raise ValueError("Workbench known_stream_id 不能超过 128 个字符。")
        return {
            "session_id": session_id,
            "known_stream_id": stream_id,
            "known_revision": known_revision,
        }

    if event_type == ClientEventType.WORKBENCH_REVIEW_REQUEST:
        session_id = str(payload.get("session_id") or "").strip()
        review_id = str(payload.get("review_id") or "").strip()
        if len(session_id) > 500:
            raise ValueError("Workbench session_id 不能超过 500 个字符。")
        if not review_id:
            raise ValueError("Workbench review_id 不能为空。")
        if len(review_id) > 500:
            raise ValueError("Workbench review_id 不能超过 500 个字符。")
        return {"session_id": session_id, "review_id": review_id}

    if event_type == ClientEventType.WORKBENCH_PROPOSAL_ACTION:
        session_id = str(payload.get("session_id") or "").strip()
        proposal_id = str(payload.get("proposal_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        decision_note = str(payload.get("decision_note") or "").strip()
        confirmed = payload.get("confirmed", False)
        if len(session_id) > 500:
            raise ValueError("Workbench session_id 不能超过 500 个字符。")
        if not proposal_id or len(proposal_id) > 128 or any(
            char in proposal_id for char in ("\x00", "\r", "\n")
        ):
            raise ValueError("Workbench proposal_id 格式无效。")
        if action not in {"approve", "reject"}:
            raise ValueError("Proposal UI action 仅支持 approve/reject。")
        if len(decision_note) > 2_000 or any(
            char in decision_note for char in ("\x00", "\r")
        ):
            raise ValueError("Proposal decision_note 格式无效。")
        if action == "reject" and not decision_note:
            raise ValueError("拒绝 Proposal 时必须填写原因。")
        if not isinstance(confirmed, bool):
            raise ValueError("Proposal confirmed 必须是布尔值。")
        return {
            "session_id": session_id,
            "proposal_id": proposal_id,
            "action": action,
            "decision_note": decision_note,
            "confirmed": confirmed,
        }

    if event_type == ClientEventType.EVOLUTION_REVIEW_REQUEST:
        action = str(payload.get("action") or "list").strip().lower()
        if action not in {"list", "detail"}:
            raise ValueError("Evolution review action 仅支持 list/detail。")
        candidate_id = str(payload.get("candidate_id") or "").strip()
        if action == "detail" and not re.fullmatch(r"evc_[0-9a-f]{24}", candidate_id):
            raise ValueError("Evolution detail candidate_id 格式无效。")
        query = str(payload.get("query") or "").strip()
        if len(query) > 256 or any(char in query for char in ("\x00", "\r", "\n")):
            raise ValueError("Evolution query 格式无效。")
        risk = str(payload.get("risk") or "").strip().lower()
        if risk not in {"", "low", "medium", "high", "critical"}:
            raise ValueError("Evolution risk 格式无效。")
        source_kind = str(payload.get("source_kind") or "").strip().lower()
        if source_kind not in {
            "", "harness_failure", "self_review_static", "user_feedback",
            "agent_interpreted_feedback",
        }:
            raise ValueError("Evolution source_kind 格式无效。")
        return {
            "action": action,
            "candidate_id": candidate_id if action == "detail" else "",
            "query": query,
            "risk": risk,
            "source_kind": source_kind,
            "limit": _bounded_int(payload.get("limit"), 50, lower=1, upper=100),
        }

    if event_type == ClientEventType.AGENTS_STOP:
        task_id = str(payload.get("task_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        reason = str(payload.get("reason") or "用户请求停止子 Agent。").strip()
        if not task_id:
            raise ValueError("Agent 停止请求缺少 task_id。")
        if len(task_id) > 500:
            raise ValueError("Agent task_id 不能超过 500 个字符。")
        if len(session_id) > 500:
            raise ValueError("Agent session_id 不能超过 500 个字符。")
        if len(reason) > 500:
            raise ValueError("Agent 停止原因不能超过 500 个字符。")
        return {
            "task_id": task_id,
            "session_id": session_id,
            "reason": reason or "用户请求停止子 Agent。",
        }

    if event_type == ClientEventType.SET_MODE:
        return {"mode": str(payload.get("mode") or "").strip().lower()}

    if event_type == ClientEventType.SET_REASONING:
        return {"enabled": _to_bool(payload.get("enabled"))}

    if event_type == ClientEventType.PERMISSION_RESPONSE:
        choice = str(payload.get("choice") or "").strip().lower()
        if choice not in {"allow_once", "deny", "grant_session", "allow", "bypass"}:
            raise ValueError(
                "权限选择无效，可用值: allow_once / deny / grant_session / bypass。"
            )
        normalized = {
            "request_id": str(payload.get("request_id") or ""),
            "choice": choice,
        }
        return normalized

    if event_type == ClientEventType.PERMISSION_REVOKE:
        grant_id = str(payload.get("grant_id") or "").strip()
        if grant_id:
            return {"grant_id": grant_id}
        scope = str(payload.get("scope") or "").strip().lower()
        if scope == "all":
            return {"scope": "all"}
        raise ValueError("撤销权限必须提供 grant_id 或 scope=all。")

    if event_type == ClientEventType.RESUME:
        normalized: dict[str, Any] = {
            "session_id": str(payload.get("session_id") or "").strip(),
        }
        if "clear" in payload:
            normalized["clear"] = _to_bool(payload.get("clear"))
        return normalized

    if event_type == ClientEventType.GOAL_PANEL:
        return {
            "limit": _bounded_int(
                payload.get("limit"),
                _PAYLOAD_LIMIT_DEFAULTS[event_type],
                lower=1,
                upper=50,
            ),
            "include_finished": _to_bool(
                payload.get("include_finished", True)
            ),
        }

    if event_type == ClientEventType.TASK_PANEL:
        detail_id = str(
            payload.get("detail_id") or payload.get("detail") or ""
        ).strip()
        normalized = {
            "limit": _bounded_int(
                payload.get("limit"),
                _PAYLOAD_LIMIT_DEFAULTS[event_type],
                lower=1,
                upper=50,
            ),
            "source": str(payload.get("source") or "all").strip().lower().replace("-", "_"),
            "status": str(payload.get("status") or "all").strip().lower().replace("-", "_"),
            "pinned": _to_bool(payload.get("pinned")),
            "refresh": _to_bool(payload.get("refresh")),
            "history": _to_bool(payload.get("history")),
        }
        if detail_id:
            normalized["detail_id"] = detail_id
        return normalized

    if event_type == ClientEventType.TASK_CANCEL:
        task_id = str(
            payload.get("task_id") or payload.get("id") or payload.get("run_id") or ""
        ).strip()
        source = str(payload.get("source") or "all").strip().lower().replace("-", "_")
        reason = str(payload.get("reason") or "用户从任务面板取消。").strip()
        if not task_id:
            raise ValueError("任务取消缺少 task_id。")
        return {
            "task_id": task_id,
            "source": source or "all",
            "reason": reason or "用户从任务面板取消。",
        }

    if event_type == ClientEventType.PERMISSIONS_PANEL:
        return {
            "limit": _bounded_int(
                payload.get("limit"),
                _PAYLOAD_LIMIT_DEFAULTS[event_type],
                lower=1,
                upper=50,
            ),
        }

    return dict(payload)


def _normalize_hello_payload(payload: dict[str, Any]) -> dict[str, Any]:
    negotiation_fields = {"minimum_version", "maximum_version", "capabilities"}
    legacy = not any(field in payload for field in negotiation_fields)
    client = str(payload.get("client") or "unknown-client").strip()
    if not client:
        client = "unknown-client"
    if len(client) > 100:
        raise ValueError("hello client 不能超过 100 个字符。")

    if legacy:
        minimum_version = PROTOCOL_VERSION
        maximum_version = PROTOCOL_VERSION
        capabilities = list(PROTOCOL_CAPABILITIES)
    else:
        minimum_version = _hello_version(payload.get("minimum_version"), "minimum_version")
        maximum_version = _hello_version(payload.get("maximum_version"), "maximum_version")
        if minimum_version > maximum_version:
            raise ValueError("hello minimum_version 不能大于 maximum_version。")
        capabilities = _hello_capabilities(payload.get("capabilities", []))

    return {
        "client": client,
        "minimum_version": minimum_version,
        "maximum_version": maximum_version,
        "capabilities": capabilities,
        "legacy": legacy,
    }


def _hello_version(raw: Any, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"hello {field} 必须是正整数。")
    return raw


def _hello_capabilities(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("hello capabilities 必须是数组。")
    if len(raw) > 100:
        raise ValueError("hello capabilities 最多包含 100 项。")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not _CAPABILITY_NAME.fullmatch(item):
            raise ValueError("hello 能力名称必须是 snake_case，且不超过 64 个字符。")
        values.add(item)
    return sorted(values)


def negotiate_hello(payload: dict[str, Any]) -> dict[str, Any]:
    """Select the highest shared protocol version and public capability intersection."""
    client_minimum = int(payload["minimum_version"])
    client_maximum = int(payload["maximum_version"])
    selected_minimum = max(client_minimum, PROTOCOL_MINIMUM_VERSION)
    selected_maximum = min(client_maximum, PROTOCOL_MAXIMUM_VERSION)
    if selected_minimum > selected_maximum:
        raise ProtocolNegotiationError(
            "协议版本不兼容："
            f"客户端支持 {client_minimum}-{client_maximum}，"
            f"当前 Naumi 支持 {PROTOCOL_MINIMUM_VERSION}-{PROTOCOL_MAXIMUM_VERSION}。"
            "请升级 Naumi 或终端 UI 后重试。",
            code="protocol_version_unsupported",
        )

    client_capabilities = set(payload.get("capabilities") or [])
    missing = sorted(set(PROTOCOL_REQUIRED_CAPABILITIES) - client_capabilities)
    if missing:
        raise ProtocolNegotiationError(
            "终端 UI 缺少运行所需协议能力："
            f"{', '.join(missing)}。请升级终端 UI 后重试。",
            code="protocol_capability_missing",
        )

    return {
        "selected_version": selected_maximum,
        "server_minimum_version": PROTOCOL_MINIMUM_VERSION,
        "server_maximum_version": PROTOCOL_MAXIMUM_VERSION,
        "capabilities": sorted(set(PROTOCOL_CAPABILITIES) & client_capabilities),
    }


def _normalize_harness_detail_request(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        run_id = validate_run_id(str(payload.get("run_id") or ""))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    raw_revision = payload.get("known_revision", 0)
    if isinstance(raw_revision, bool):
        raise ValueError("Harness known_revision 必须是非负整数。")
    try:
        known_revision = int(raw_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("Harness known_revision 必须是非负整数。") from exc
    if known_revision < 0 or known_revision > 2_147_483_647:
        raise ValueError("Harness known_revision 必须是非负整数。")
    return {
        "run_id": run_id,
        "known_revision": known_revision,
    }


def _bounded_int(raw: Any, default: int, *, lower: int, upper: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(value, upper))


def _normalize_text_list(
    raw: Any,
    *,
    field: str,
    max_items: int,
    max_chars: int,
) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{field} 必须是数组。")
    if len(raw) > max_items:
        raise ValueError(f"{field} 最多包含 {max_items} 项。")
    normalized: list[str] = []
    for value in raw:
        text = str(value).strip()
        if not text:
            continue
        if len(text) > max_chars:
            raise ValueError(f"{field} 单项不能超过 {max_chars} 个字符。")
        normalized.append(text)
    return normalized


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False
