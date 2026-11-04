"""Real SQLite message identity, single dispatch and hash-chain evidence tests."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from naumi_agent.personal.message_store import PersonalMessageStore
from naumi_agent.personal.models import PersonalAgentError

A, B, C = ("pa_" + value * 32 for value in "abc")
COLLAB = "pc_" + "d" * 32
WORKSPACE = "workspace-one"
MESSAGE = "pm_" + "e" * 32


def request(store: PersonalMessageStore, **changes):
    return store.create(
        **{
            "workspace_key": WORKSPACE,
            "collaboration_id": COLLAB,
            "step_id": "research",
            "source_session_id": "sender-session",
            "sender_agent_id": A,
            "sender_version": 1,
            "recipient_agent_id": B,
            "recipient_version": 2,
            "request_id": "request-one",
            "content": "请读取明确指定的预算资料。\n保留来源。",
            "context": "只读 budget.csv",
            "tool_names": ["file_read", "personal_message"],
            **changes,
        }
    )


def claim(store: PersonalMessageStore, message: dict, **changes):
    return store.claim(
        message["id"],
        WORKSPACE,
        **{
            "root_job_id": "agent-job-" + "1" * 64,
            "root_owner_id": "root-owner",
            "root_claim_epoch": 1,
            "source_run_id": "source-run",
            "dispatch_token": "dispatch-one",
            **changes,
        },
    )


def complete(store: PersonalMessageStore, message: dict, **changes):
    return store.complete(
        message["id"],
        WORKSPACE,
        **{
            "dispatch_token": "dispatch-one",
            "receiver_run_id": "receiver-run",
            "outcome": "completed",
            "response": "剩余预算 825 元，来源 budget.csv。",
            "error": "",
            "usage": {"input_tokens": 9, "output_tokens": 7, "cost_usd": 0.01},
            "run_receipt": {"run_id": "receiver-run", "outcome": "completed", "evidence": ["file"]},
            **changes,
        },
    )


def assert_error(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    assert caught.value.code == code
    assert any("\u4e00" <= char <= "\u9fff" for char in caught.value.message)


def test_lazy_construction_and_readonly_lookups_do_not_create_databases(tmp_path):
    base = tmp_path / "personal"
    store = PersonalMessageStore(base)
    assert store.base_dir == base and store.db_path == base / "personal_messages.db"
    assert store.list(WORKSPACE, COLLAB) == []
    assert store.lookup_session(WORKSPACE, "missing") is None
    assert_error("not_found", lambda: store.get(MESSAGE, WORKSPACE))
    assert_error("not_found", lambda: store.events(MESSAGE, WORKSPACE))
    assert not base.exists()


def test_identity_payload_and_current_receipts_survive_restart(tmp_path):
    store = PersonalMessageStore(tmp_path)
    first = request(store)
    assert first["id"].startswith("pm_") and len(first["id"]) == 35
    assert len(first["receiver_session_id"]) == 32
    assert first["receiver_task_id"] == "peer-" + first["id"][3:]
    assert first["source_session_id"] != first["receiver_session_id"]
    assert first["depth"] == 0 and first["origin"] == "agent"
    for key in (
        "dispatch_token",
        "root_job_id",
        "root_owner_id",
        "source_run_id",
        "receiver_run_id",
        "response",
        "response_sha256",
        "result_sha256",
        "error",
        "outcome",
        "acknowledged_at",
        "acknowledged_by",
    ):
        assert first[key] == ""
    assert first["root_claim_epoch"] == 0 and len(first["payload_sha256"]) == 64
    assert PersonalMessageStore(tmp_path).get(first["id"], WORKSPACE) == first
    assert request(store) == first
    assert claim(store, first)
    done = complete(store, first)
    ack = store.acknowledge(
        first["id"], WORKSPACE, sender_agent_id=A, expected_response_sha256=done["response_sha256"]
    )
    assert request(PersonalMessageStore(tmp_path)) == ack
    assert ack["response"] == done["response"] and ack["acknowledged_by"] == "agent"
    assert store.lookup_session(WORKSPACE, first["receiver_session_id"]) == ack
    assert store.lookup_session("other", first["receiver_session_id"]) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "另一条消息"},
        {"context": "另一份共享资料"},
        {"sender_agent_id": C},
        {"sender_version": 2},
        {"recipient_agent_id": C},
        {"recipient_version": 3},
        {"tool_names": ["file_read"]},
        {"collaboration_id": "pc_" + "f" * 32},
        {"step_id": "other"},
        {"parent_message_id": MESSAGE},
        {"origin": "manual"},
    ],
)
def test_request_id_is_payload_bound(tmp_path, changes):
    store = PersonalMessageStore(tmp_path)
    original = request(store)
    assert_error("idempotency_conflict", lambda: request(store, **changes))
    assert store.get(original["id"], WORKSPACE) == original


def test_normalized_tools_and_scoped_request_namespaces(tmp_path):
    store = PersonalMessageStore(tmp_path)
    one = request(store, tool_names=["personal_message", "file_read"])
    assert request(store)["id"] == one["id"]
    two = request(store, workspace_key="workspace-two")
    three = request(store, source_session_id="another-session")
    assert len({one["id"], two["id"], three["id"]}) == 3
    assert store.list(WORKSPACE, COLLAB) == [one, three]
    assert store.list("workspace-two", COLLAB) == [two]


def test_inputs_outputs_and_receipts_are_detached(tmp_path):
    store = PersonalMessageStore(tmp_path)
    names = ["file_read"]
    message = request(store, tool_names=names)
    names.append("file_write")
    message["tool_names"].append("file_edit")
    assert store.get(message["id"], WORKSPACE)["tool_names"] == ["file_read"]
    assert claim(store, message)
    usage = {"input_tokens": 10, "models": ["test"]}
    receipt = {"run_id": "receiver-run", "evidence": ["real"]}
    saved = complete(store, message, usage=usage, run_receipt=receipt)
    expected = copy.deepcopy(saved)
    usage["models"].append("changed")
    receipt["evidence"].clear()
    saved["run_receipt"]["evidence"].clear()
    events = store.events(message["id"], WORKSPACE)
    events[-1]["data"]["outcome"] = "changed"
    assert store.get(message["id"], WORKSPACE) == expected
    assert store.events(message["id"], WORKSPACE)[-1]["data"]["outcome"] == "completed"


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_key": ""},
        {"workspace_key": "a" * 2049},
        {"workspace_key": None},
        {"collaboration_id": "wrong"},
        {"step_id": ""},
        {"step_id": "with space"},
        {"source_session_id": "s" * 129},
        {"sender_agent_id": "wrong"},
        {"recipient_agent_id": "pa_" + "A" * 32},
        {"sender_version": True},
        {"sender_version": 0},
        {"recipient_version": 1.0},
        {"recipient_version": 2**31},
        {"request_id": ""},
        {"request_id": "with space"},
        {"request_id": "/first"},
        {"content": " "},
        {"content": 1},
        {"content": "x" * 16001},
        {"context": "x" * 32001},
        {"context": "x\x00"},
        {"content": "x\x1b[0m"},
        {"content": "\ud800"},
        {"tool_names": ("file_read",)},
        {"tool_names": ["file_read", "file_read"]},
        {"tool_names": ["bad tool"]},
        {"tool_names": ["x"] * 129},
        {"parent_message_id": "wrong"},
        {"origin": "llm"},
    ],
)
def test_create_rejects_invalid_inputs_before_writing(tmp_path, changes):
    store = PersonalMessageStore(tmp_path / "new")
    assert_error("invalid_input", lambda: request(store, **changes))
    assert not store.db_path.exists()


def test_exact_whitespace_and_boundary_sizes_are_preserved(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(
        store,
        content=" " + "中" * 15998 + "\n",
        context=" " * 32000,
        tool_names=[],
        origin="manual",
    )
    assert len(message["content"]) == 16000 and message["context"] == " " * 32000
    assert message["content"].startswith(" ") and message["content"].endswith("\n")
    assert message["origin"] == "manual"


def test_foreign_scope_and_missing_messages_never_leak_payloads(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert_error("not_found", lambda: store.get(message["id"], "other"))
    assert_error("not_found", lambda: store.events(message["id"], "other"))
    assert_error(
        "not_found",
        lambda: store.mark_received(
            message["id"],
            "other",
            dispatch_token="token",
            receiver_run_id="run",
        ),
    )
    assert store.list("other", COLLAB) == []
    assert store.list(WORKSPACE, "pc_" + "f" * 32) == []
    assert store.list(WORKSPACE, COLLAB, agent_id=C) == []
    assert store.list(WORKSPACE, COLLAB, agent_id=A) == [message]
    assert store.list(WORKSPACE, COLLAB, agent_id=B) == [message]


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"offset": -1},
        {"offset": 1.0},
        {"offset": 2**63},
        {"agent_id": "invalid"},
    ],
)
def test_list_bounds_are_strict(tmp_path, changes):
    store = PersonalMessageStore(tmp_path)
    assert_error("invalid_input", lambda: store.list(WORKSPACE, COLLAB, **changes))


def test_list_pagination_keeps_chronological_order(tmp_path):
    store = PersonalMessageStore(tmp_path)
    messages = [request(store, request_id=f"request-{n}") for n in range(4)]
    assert store.list(WORKSPACE, COLLAB, limit=2) == messages[:2]
    assert store.list(WORKSPACE, COLLAB, limit=2, offset=2) == messages[2:]


def test_dispatch_claim_is_irreversible_and_root_binding_is_exact(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message) is True
    first = store.get(message["id"], WORKSPACE)
    assert claim(PersonalMessageStore(tmp_path), message) is False
    assert claim(store, message, dispatch_token="another-token") is False
    assert store.get(message["id"], WORKSPACE) == first
    for changes in (
        {"root_job_id": "another-job"},
        {"root_owner_id": "another-owner"},
        {"root_claim_epoch": 2},
        {"source_run_id": "another-run"},
    ):
        assert_error("binding_conflict", lambda changes=changes: claim(store, message, **changes))
    events = store.events(message["id"], WORKSPACE)
    assert [e["kind"] for e in events] == ["requested", "dispatch_started"]
    assert "dispatch-one" not in json.dumps(events)


@pytest.mark.parametrize(
    "changes",
    [
        {"root_job_id": ""},
        {"root_owner_id": "bad owner"},
        {"root_claim_epoch": 0},
        {"root_claim_epoch": True},
        {"source_run_id": ""},
        {"dispatch_token": "bad token"},
    ],
)
def test_claim_rejects_invalid_binding_fields(tmp_path, changes):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert_error("invalid_input", lambda: claim(store, message, **changes))
    assert len(store.events(message["id"], WORKSPACE)) == 1


def test_concurrent_create_and_dispatch_have_one_winner_across_store_instances(tmp_path):
    def create_one(_):
        return request(PersonalMessageStore(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        messages = list(pool.map(create_one, range(16)))
    assert len({m["id"] for m in messages}) == 1

    def claim_one(n):
        return claim(PersonalMessageStore(tmp_path), messages[0], dispatch_token=f"token-{n}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = list(pool.map(claim_one, range(16)))
    assert winners.count(True) == 1
    message = PersonalMessageStore(tmp_path).get(messages[0]["id"], WORKSPACE)
    assert message["dispatch_token"] == f"token-{winners.index(True)}"
    assert len(PersonalMessageStore(tmp_path).events(message["id"], WORKSPACE)) == 2


def test_received_binding_is_once_only_and_requires_actual_dispatch_token(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert_error(
        "binding_conflict",
        lambda: store.mark_received(
            message["id"],
            WORKSPACE,
            dispatch_token="dispatch-one",
            receiver_run_id="receiver-run",
        ),
    )
    assert claim(store, message)
    assert_error(
        "binding_conflict",
        lambda: store.mark_received(
            message["id"],
            WORKSPACE,
            dispatch_token="wrong",
            receiver_run_id="receiver-run",
        ),
    )
    received = store.mark_received(
        message["id"], WORKSPACE, dispatch_token="dispatch-one", receiver_run_id="receiver-run"
    )
    assert received["received_at"] and received["receiver_run_id"] == "receiver-run"
    assert (
        store.mark_received(
            message["id"], WORKSPACE, dispatch_token="dispatch-one", receiver_run_id="receiver-run"
        )
        == received
    )
    assert_error(
        "binding_conflict",
        lambda: store.mark_received(
            message["id"],
            WORKSPACE,
            dispatch_token="dispatch-one",
            receiver_run_id="other-run",
        ),
    )


def test_complete_atomically_binds_received_run_exact_body_and_receipt(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    result = complete(store, message)
    assert result["response_sha256"] == hashlib.sha256(result["response"].encode()).hexdigest()
    assert len(result["result_sha256"]) == 64 and result["completed_at"]
    assert [e["kind"] for e in store.events(message["id"], WORKSPACE)] == [
        "requested",
        "dispatch_started",
        "received",
        "result",
    ]
    assert complete(PersonalMessageStore(tmp_path), message) == result
    assert (
        store.mark_received(
            message["id"], WORKSPACE, dispatch_token="dispatch-one", receiver_run_id="receiver-run"
        )
        == result
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"response": "changed"}, "idempotency_conflict"),
        ({"error": "changed"}, "idempotency_conflict"),
        ({"usage": {"input_tokens": 99}}, "idempotency_conflict"),
        ({"outcome": "error"}, "idempotency_conflict"),
        ({"run_receipt": {"run_id": "receiver-run", "extra": True}}, "idempotency_conflict"),
        ({"receiver_run_id": "other", "run_receipt": {"run_id": "other"}}, "binding_conflict"),
        ({"dispatch_token": "other"}, "binding_conflict"),
    ],
)
def test_result_replay_cannot_replace_original(tmp_path, changes, code):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    original = complete(store, message)
    assert_error(code, lambda: complete(store, message, **changes))
    assert store.get(message["id"], WORKSPACE) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "running"},
        {"outcome": []},
        {"response": None},
        {"response": "x" * (2 * 1024**2 + 1)},
        {"response": "中" * 700000},
        {"response": "bad\ud800"},
        {"error": "x" * 16001},
        {"usage": []},
        {"usage": {"bad": math.nan}},
        {"usage": {"bad": (1, 2)}},
        {"usage": {1: "invalid key"}},
        {"run_receipt": None},
        {"run_receipt": {"run_id": "other"}},
        {"receiver_run_id": ""},
        {"usage": {"bad": "x" * 17000}},
    ],
)
def test_complete_rejects_invalid_or_unbound_results(tmp_path, changes):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    assert_error("invalid_input", lambda: complete(store, message, **changes))
    assert store.get(message["id"], WORKSPACE)["outcome"] == ""
    assert len(store.events(message["id"], WORKSPACE)) == 2


@pytest.mark.parametrize("outcome", ["error", "timeout", "cancelled", "unknown"])
def test_noncompleted_delivery_never_becomes_completed_or_acknowledged(tmp_path, outcome):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    result = complete(
        store,
        message,
        outcome=outcome,
        receiver_run_id="",
        run_receipt={},
        response="",
        error="执行未形成可信完成结果",
    )
    assert result["outcome"] == outcome and not result["receiver_run_id"]
    assert len(store.events(message["id"], WORKSPACE)) == 3
    assert_error(
        "message_not_ready",
        lambda: store.acknowledge(
            message["id"],
            WORKSPACE,
            sender_agent_id=A,
            expected_response_sha256=result["response_sha256"],
        ),
    )
    assert_error(
        "binding_conflict",
        lambda: store.mark_received(
            message["id"],
            WORKSPACE,
            dispatch_token="dispatch-one",
            receiver_run_id="late-run",
        ),
    )
    assert_error("idempotency_conflict", lambda: complete(store, message))


@pytest.mark.parametrize("first,second", [("agent", "user"), ("user", "agent")])
def test_acknowledgement_is_digest_bound_and_preserves_actual_first_actor(tmp_path, first, second):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    done = complete(store, message)
    assert_error(
        "message_scope_denied",
        lambda: store.acknowledge(
            message["id"],
            WORKSPACE,
            sender_agent_id=B,
            expected_response_sha256=done["response_sha256"],
        ),
    )
    assert_error(
        "binding_conflict",
        lambda: store.acknowledge(
            message["id"],
            WORKSPACE,
            sender_agent_id=A,
            expected_response_sha256="0" * 64,
        ),
    )
    ack = store.acknowledge(
        message["id"],
        WORKSPACE,
        sender_agent_id=A,
        expected_response_sha256=done["response_sha256"],
        actor_kind=first,
    )
    assert ack["acknowledged_at"] and ack["acknowledged_by"] == first
    assert (
        store.acknowledge(
            message["id"],
            WORKSPACE,
            sender_agent_id=A,
            expected_response_sha256=done["response_sha256"],
            actor_kind=second,
        )
        == ack
    )
    assert complete(store, message) == ack
    assert len(store.events(message["id"], WORKSPACE)) == 5


def test_concurrent_result_and_ack_replays_do_not_duplicate_receipts(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: complete(PersonalMessageStore(tmp_path), message), range(12))
        )
    assert all(result == results[0] for result in results)
    with ThreadPoolExecutor(max_workers=8) as pool:
        acks = list(
            pool.map(
                lambda _: PersonalMessageStore(tmp_path).acknowledge(
                    message["id"],
                    WORKSPACE,
                    sender_agent_id=A,
                    expected_response_sha256=results[0]["response_sha256"],
                ),
                range(12),
            )
        )
    assert all(ack == acks[0] for ack in acks)
    assert len(store.events(message["id"], WORKSPACE)) == 5


def nested(store, parent, *, recipient=C, content="请提供进一步明确资料", **changes):
    return request(
        store,
        source_session_id=parent["receiver_session_id"],
        sender_agent_id=parent["recipient_agent_id"],
        sender_version=parent["recipient_version"],
        recipient_agent_id=recipient,
        request_id="nested-" + parent["id"],
        parent_message_id=parent["id"],
        content=content,
        **changes,
    )


def test_distinct_bidirectional_questions_use_fresh_sessions_without_false_cycle_rejection(
    tmp_path,
):
    store = PersonalMessageStore(tmp_path)
    first = request(store)
    second = nested(store, first, recipient=A, content="请补充统计月份")
    third = nested(store, second, recipient=B, content="请按已补充月份重新核算")
    assert [first["depth"], second["depth"], third["depth"]] == [0, 1, 2]
    assert len({m["receiver_session_id"] for m in [first, second, third]}) == 3
    assert_error(
        "peer_cycle",
        lambda: nested(
            store, third, recipient=B, content=first["content"], context=first["context"]
        ),
    )


def test_parent_scope_sender_and_receiving_session_cannot_be_forged(tmp_path):
    store = PersonalMessageStore(tmp_path)
    first = request(store)
    for changes in (
        {"collaboration_id": "pc_" + "f" * 32},
        {"step_id": "other"},
        {"sender_agent_id": A},
        {"sender_version": 7},
        {"source_session_id": "other-session"},
    ):
        fields = {
            "source_session_id": first["receiver_session_id"],
            "sender_agent_id": B,
            "sender_version": first["recipient_version"],
            "parent_message_id": first["id"],
            "request_id": "nested",
            **changes,
        }
        assert_error("binding_conflict", lambda fields=fields: request(store, **fields))
    assert_error(
        "not_found", lambda: request(store, parent_message_id=MESSAGE, request_id="missing")
    )


def test_nested_claim_uses_same_root_lease_and_actual_receiving_run(tmp_path):
    store = PersonalMessageStore(tmp_path)
    first = request(store)
    second = nested(store, first)
    assert claim(store, first)
    assert_error("binding_conflict", lambda: claim(store, second))
    store.mark_received(
        first["id"], WORKSPACE, dispatch_token="dispatch-one", receiver_run_id="first-receiver-run"
    )
    assert_error(
        "binding_conflict",
        lambda: claim(store, second, root_job_id="other-job", source_run_id="first-receiver-run"),
    )
    assert claim(store, second, source_run_id="first-receiver-run")


def test_depth_quota_and_idempotent_replay_remain_correct(tmp_path):
    store = PersonalMessageStore(tmp_path)
    current = request(store)
    for n in range(1, 9):
        current = nested(store, current, recipient=A if n % 2 else B, content=f"独立问题{n}")
        assert current["depth"] == n
    assert_error("peer_depth", lambda: nested(store, current, content="第九层问题"))
    for n in range(23):
        request(store, request_id=f"additional-{n}")
    assert len(store.list(WORKSPACE, COLLAB, limit=500)) == 32
    assert request(store)["id"] == store.list(WORKSPACE, COLLAB)[0]["id"]
    assert_error("peer_limit", lambda: request(store, request_id="overflow"))
    assert request(store, step_id="other", request_id="other-step")["depth"] == 0


def test_concurrent_new_requests_cannot_exceed_quota(tmp_path):
    def create_one(n):
        try:
            return request(PersonalMessageStore(tmp_path), request_id=f"request-{n}")["id"]
        except PersonalAgentError as exc:
            assert exc.code == "peer_limit"
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create_one, range(40)))
    assert len({value for value in results if value is not None}) == 32
    assert results.count(None) == 8


@pytest.mark.parametrize(
    "tamper",
    [
        "request_body",
        "request_digest",
        "generated_identity",
        "dispatch_token",
        "result_body",
        "result_digest",
        "result_shape",
        "event_body",
        "event_digest",
        "event_previous",
        "remove_first",
        "remove_middle",
        "remove_tail",
        "anchor_count",
        "anchor_head",
    ],
)
def test_tampered_requests_results_or_receipt_chain_fail_closed(tmp_path, tamper):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    assert claim(store, message)
    result = complete(store, message)
    store.acknowledge(
        message["id"],
        WORKSPACE,
        sender_agent_id=A,
        expected_response_sha256=result["response_sha256"],
    )
    with sqlite3.connect(store.db_path) as db:
        raw = db.execute("SELECT request_json,result_json FROM peer_messages").fetchone()
        if tamper in {"request_body", "generated_identity"}:
            request_json = json.loads(raw[0])
            request_json["content" if tamper == "request_body" else "receiver_task_id"] = "changed"
            db.execute("UPDATE peer_messages SET request_json=?", (json.dumps(request_json),))
        elif tamper == "request_digest":
            db.execute("UPDATE peer_messages SET payload_sha256=?", ("0" * 64,))
        elif tamper == "dispatch_token":
            db.execute("UPDATE peer_messages SET dispatch_token='other'")
        elif tamper.startswith("result_"):
            result_json = json.loads(raw[1])
            result_json[
                {
                    "result_body": "response",
                    "result_digest": "result_sha256",
                    "result_shape": "run_receipt",
                }[tamper]
            ] = [] if tamper == "result_shape" else "changed"
            db.execute("UPDATE peer_messages SET result_json=?", (json.dumps(result_json),))
        elif tamper in {"event_body", "event_digest", "event_previous"}:
            column = {
                "event_body": "data_json",
                "event_digest": "receipt_sha256",
                "event_previous": "previous_sha256",
            }[tamper]
            db.execute(f"UPDATE peer_message_receipts SET {column}=? WHERE sequence=3", ("{}",))
        elif tamper.startswith("remove_"):
            n = {"remove_first": 1, "remove_middle": 3, "remove_tail": 5}[tamper]
            db.execute("DELETE FROM peer_message_receipts WHERE sequence=?", (n,))
        elif tamper == "anchor_count":
            db.execute("UPDATE peer_messages SET receipt_count=0")
        else:
            db.execute("UPDATE peer_messages SET receipt_head_sha256=?", ("0" * 64,))
    for operation in (
        lambda: store.get(message["id"], WORKSPACE),
        lambda: store.events(message["id"], WORKSPACE),
        lambda: store.list(WORKSPACE, COLLAB),
        lambda: store.lookup_session(WORKSPACE, message["receiver_session_id"]),
        lambda: request(store),
        lambda: claim(store, message),
    ):
        assert_error("storage_error", operation)


def test_future_schema_is_never_downgraded_or_written(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA user_version=99")
    newer = PersonalMessageStore(tmp_path)
    assert_error("unsupported_schema", lambda: newer.get(message["id"], WORKSPACE))
    assert_error("unsupported_schema", lambda: request(newer, request_id="new"))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT COUNT(*) FROM peer_messages").fetchone()[0] == 1


def test_readonly_lookup_has_no_write_side_effects_and_store_is_wal(tmp_path):
    store = PersonalMessageStore(tmp_path)
    message = request(store)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        columns = {row[1] for row in db.execute("PRAGMA table_info(peer_messages)")}
        assert not {"status", "state", "claim_expires_at"} & columns
        before = db.execute("SELECT * FROM peer_messages").fetchall()
        receipts = db.execute("SELECT * FROM peer_message_receipts").fetchall()
    assert PersonalMessageStore(tmp_path).lookup_session(WORKSPACE, message["receiver_session_id"])
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT * FROM peer_messages").fetchall() == before
        assert db.execute("SELECT * FROM peer_message_receipts").fetchall() == receipts
