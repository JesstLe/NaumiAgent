from __future__ import annotations

import json
import sys

import pytest

from naumi_agent.ui.permission_confirmation import summarize_arguments
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    normalize_client_record,
)


def _assert_bounded_strict_json(summary: dict[str, object]) -> str:
    encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
    assert len(encoded) <= 1200
    assert json.loads(encoded) == summary
    return encoded


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("allow_once", "allow_once"),
        ("deny", "deny"),
        ("grant_session", "grant_session"),
        ("allow", "allow"),
        ("bypass", "bypass"),
    ],
)
def test_permission_choice_normalization_preserves_supported_choices(
    choice: str,
    expected: str,
) -> None:
    payload: dict[str, object] = {"request_id": 123, "choice": choice.upper()}

    record = normalize_client_record(
        {"type": ClientEventType.PERMISSION_RESPONSE, "payload": payload}
    )

    assert record["payload"] == {
        "request_id": "123",
        "choice": expected,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"grant_id": 42}, {"grant_id": "42"}),
        ({"scope": "ALL"}, {"scope": "all"}),
    ],
)
def test_permission_revoke_accepts_one_grant_or_all(
    payload: dict[str, object], expected: dict[str, str]
) -> None:
    record = normalize_client_record(
        {"type": ClientEventType.PERMISSION_REVOKE, "payload": payload}
    )

    assert record["payload"] == expected


@pytest.mark.parametrize("payload", [{}, {"grant_id": " "}, {"scope": "session"}])
def test_permission_revoke_rejects_empty_or_unsupported_scope(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="撤销权限"):
        normalize_client_record(
            {"type": ClientEventType.PERMISSION_REVOKE, "payload": payload}
        )


def test_protocol_exposes_permission_grant_events() -> None:
    assert ServerEventType.PERMISSION_GRANTS_CHANGED == "permission/grants_changed"


def test_protocol_exposes_typed_harness_receipt_event() -> None:
    assert ServerEventType.HARNESS_RECEIPT == "harness/receipt"


def test_protocol_normalizes_queue_promotion_target() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.QUEUE_PROMOTE,
            "payload": {"target_request_id": "  submit-later  ", "private": "drop"},
        }
    )

    assert record["payload"] == {"target_request_id": "submit-later"}


def test_protocol_normalizes_queue_cancel_target() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.QUEUE_CANCEL,
            "payload": {"target_request_id": "  submit-cancel  ", "private": "drop"},
        }
    )

    assert record["payload"] == {"target_request_id": "submit-cancel"}


def test_protocol_normalizes_interaction_cancel_target() -> None:
    record = normalize_client_record({
        "type": ClientEventType.INTERACTION_CANCEL,
        "payload": {"interaction_id": " ask-goal-cancel ", "private": "drop"},
    })

    assert record["payload"] == {"interaction_id": "ask-goal-cancel"}


@pytest.mark.parametrize("interaction_id", ["", "ask-", "invalid", "ask-空白"])
def test_protocol_rejects_invalid_interaction_cancel_target(interaction_id: str) -> None:
    with pytest.raises(ValueError, match="interaction_id"):
        normalize_client_record({
            "type": ClientEventType.INTERACTION_CANCEL,
            "payload": {"interaction_id": interaction_id},
        })


@pytest.mark.parametrize("target", [None, "", " ", "x" * 201])
def test_protocol_rejects_invalid_queue_promotion_target(target: object) -> None:
    with pytest.raises(ValueError, match="target_request_id"):
        normalize_client_record(
            {
                "type": ClientEventType.QUEUE_PROMOTE,
                "payload": {"target_request_id": target},
            }
        )


@pytest.mark.parametrize("target", [None, "", " ", "x" * 201])
def test_protocol_rejects_invalid_queue_cancel_target(target: object) -> None:
    with pytest.raises(ValueError, match="target_request_id"):
        normalize_client_record(
            {
                "type": ClientEventType.QUEUE_CANCEL,
                "payload": {"target_request_id": target},
            }
        )


def test_protocol_normalizes_harness_eval_batch_request() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.HARNESS_EVAL_BATCH_REQUEST,
            "payload": {
                "suite_id": "surface-protocol",
                "repetitions": 5,
                "batch_id": "candidate:1",
            },
        }
    )

    assert record["payload"] == {
        "suite_id": "surface-protocol",
        "repetitions": 5,
        "batch_id": "candidate:1",
    }


def test_protocol_normalizes_harness_eval_promotion_request() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.HARNESS_EVAL_PROMOTION_REQUEST,
            "payload": {
                "suite_id": "surface-protocol",
                "batch_id": "candidate:1",
                "reason": "  完整回归已通过  ",
            },
        }
    )

    assert record["payload"] == {
        "suite_id": "surface-protocol",
        "batch_id": "candidate:1",
        "reason": "完整回归已通过",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"suite_id": "Upper", "batch_id": "candidate-1"},
        {"suite_id": "surface-protocol", "batch_id": "../candidate"},
        {"suite_id": "surface-protocol", "batch_id": "candidate-1", "reason": "短"},
    ],
)
def test_protocol_rejects_invalid_harness_eval_promotion_request(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="Harness Eval Promotion"):
        normalize_client_record(
            {
                "type": ClientEventType.HARNESS_EVAL_PROMOTION_REQUEST,
                "payload": payload,
            }
        )


def test_protocol_normalizes_workbench_snapshot_requests() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.WORKBENCH_REQUEST,
            "payload": {
                "session_id": 42,
                "known_stream_id": " stream-a ",
                "known_revision": "7",
            },
        }
    )

    assert record["payload"] == {
        "session_id": "42",
        "known_stream_id": "stream-a",
        "known_revision": 7,
    }


def test_protocol_normalizes_typed_interaction_response() -> None:
    record = normalize_client_record({
        "type": ClientEventType.INTERACTION_RESPONSE,
        "payload": {
            "request_id": " ask-response-1 ",
            "kind": " OPTION ",
            "value": " safe ",
            "private_payload": "drop",
        },
    })

    assert record["payload"] == {
        "request_id": "ask-response-1",
        "kind": "option",
        "value": "safe",
        "custom_text": "",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": "bad", "kind": "option", "value": "safe"},
        {"request_id": "ask-1", "kind": "option", "value": ""},
        {
            "request_id": "ask-1", "kind": "option", "value": "safe",
            "custom_text": "also custom",
        },
        {"request_id": "ask-1", "kind": "custom", "custom_text": ""},
        {"request_id": "ask-1", "kind": "unknown", "value": "safe"},
    ],
)
def test_protocol_rejects_invalid_interaction_response_combinations(payload) -> None:
    with pytest.raises(ValueError, match="交互响应|选项响应|自定义输入"):
        normalize_client_record({
            "type": ClientEventType.INTERACTION_RESPONSE,
            "payload": payload,
        })


def test_protocol_normalizes_workbench_review_requests() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.WORKBENCH_REVIEW_REQUEST,
            "payload": {"session_id": " session-1 ", "review_id": " approval-1 "},
        }
    )

    assert record["payload"] == {
        "session_id": "session-1",
        "review_id": "approval-1",
    }


def test_protocol_normalizes_workbench_proposal_actions() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {
                "session_id": " session-1 ",
                "proposal_id": " proposal-1 ",
                "action": " REJECT ",
                "decision_note": " 证据不足 ",
                "confirmed": True,
            },
        }
    )

    assert record["payload"] == {
        "session_id": "session-1",
        "proposal_id": "proposal-1",
        "action": "reject",
        "decision_note": "证据不足",
        "confirmed": True,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"proposal_id": "proposal-1", "action": "defer"},
        {"proposal_id": "proposal-1", "action": "reject", "decision_note": ""},
        {"proposal_id": "", "action": "approve"},
        {"proposal_id": "proposal-1", "action": "approve", "confirmed": "yes"},
    ],
)
def test_protocol_rejects_invalid_workbench_proposal_actions(payload: dict) -> None:
    with pytest.raises(ValueError):
        normalize_client_record(
            {
                "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
                "payload": payload,
            }
        )


@pytest.mark.parametrize("review_id", ["", " " * 3, "x" * 501])
def test_protocol_rejects_invalid_workbench_review_ids(review_id: str) -> None:
    with pytest.raises(ValueError, match="review_id"):
        normalize_client_record(
            {
                "type": ClientEventType.WORKBENCH_REVIEW_REQUEST,
                "payload": {"review_id": review_id},
            }
        )


@pytest.mark.parametrize("revision", [True, -1, 2_147_483_648, "unknown"])
def test_protocol_rejects_invalid_workbench_revisions(revision: object) -> None:
    with pytest.raises(ValueError, match="known_revision"):
        normalize_client_record(
            {
                "type": ClientEventType.WORKBENCH_REQUEST,
                "payload": {"known_revision": revision},
            }
        )


def test_protocol_normalizes_harness_detail_requests() -> None:
    for event_type in (
        ClientEventType.HARNESS_EXPLAIN_REQUEST,
        ClientEventType.HARNESS_REPLAY_REQUEST,
    ):
        record = normalize_client_record(
            {
                "type": event_type,
                "payload": {"run_id": "run:detail-1", "known_revision": "3"},
            }
        )

        assert record["payload"] == {
            "run_id": "run:detail-1",
            "known_revision": 3,
        }


def test_protocol_normalizes_harness_eval_baseline_request() -> None:
    record = normalize_client_record(
        {
            "type": ClientEventType.HARNESS_EVAL_BASELINE_REQUEST,
            "payload": {"suite_id": "surface-protocol"},
        }
    )

    assert record["payload"] == {"suite_id": "surface-protocol"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {"limit": 20, "include_finished": True}),
        (
            {"limit": 500, "include_finished": False},
            {"limit": 50, "include_finished": False},
        ),
        (
            {"limit": "7", "include_finished": "false"},
            {"limit": 7, "include_finished": False},
        ),
    ],
)
def test_protocol_normalizes_goal_panel_request(payload, expected) -> None:
    record = normalize_client_record(
        {"type": ClientEventType.GOAL_PANEL, "payload": payload}
    )

    assert record["payload"] == expected


@pytest.mark.parametrize("suite_id", ["", "Upper", "../other", "x" * 65])
def test_protocol_rejects_invalid_harness_eval_suite_id(suite_id: str) -> None:
    with pytest.raises(ValueError, match="suite_id"):
        normalize_client_record(
            {
                "type": ClientEventType.HARNESS_EVAL_BASELINE_REQUEST,
                "payload": {"suite_id": suite_id},
            }
        )


@pytest.mark.parametrize("run_id", ["", " ", "../other", "x" * 129])
def test_protocol_rejects_invalid_harness_detail_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        normalize_client_record(
            {
                "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
                "payload": {"run_id": run_id},
            }
        )


@pytest.mark.parametrize("revision", [True, -1, 2_147_483_648, "unknown"])
def test_protocol_rejects_invalid_harness_detail_revisions(revision: object) -> None:
    with pytest.raises(ValueError, match="known_revision"):
        normalize_client_record(
            {
                "type": ClientEventType.HARNESS_REPLAY_REQUEST,
                "payload": {
                    "run_id": "run:detail-1",
                    "known_revision": revision,
                },
            }
        )


def test_argument_summary_redacts_and_bounds_output() -> None:
    summary = summarize_arguments(
        {
            "command": "x" * 400,
            "authorization": "Bearer private",
            "nested": {"password": "private"},
            "items": list(range(80)),
            "opaque": object(),
        }
    )

    assert summary["authorization"] == "[已隐藏]"
    assert summary["nested"]["password"] == "[已隐藏]"
    assert len(summary["command"]) <= 160
    assert len(summary["items"]) == 50
    assert summary["opaque"] == "<object>"
    assert len(json.dumps(summary, ensure_ascii=False)) <= 1200


@pytest.mark.parametrize("length", [160, 161])
def test_argument_summary_caps_strings_at_the_exact_contract_boundary(length: int) -> None:
    value = "x" * length

    summary = summarize_arguments({"value": value})

    assert summary["value"] == value[:160]
    assert len(summary["value"]) == 160


@pytest.mark.parametrize("count", [50, 51])
def test_argument_summary_caps_collections_at_the_exact_contract_boundary(count: int) -> None:
    summary = summarize_arguments({"items": list(range(count))})

    assert summary["items"] == list(range(50))
    assert len(summary["items"]) == 50


def test_argument_summary_caps_dynamically_long_type_placeholders() -> None:
    opaque = type("Opaque" * 40, (), {})()

    summary = summarize_arguments({"opaque": opaque})

    assert summary["opaque"].startswith("<Opaque")
    assert len(summary["opaque"]) == 160


@pytest.mark.parametrize("value_width", [10, 11])
def test_argument_summary_uses_the_ordinary_json_contract_ceiling(value_width: int) -> None:
    arguments = {f"key_{index:02d}": "v" * value_width for index in range(50)}

    summary = summarize_arguments(arguments)

    assert len(json.dumps(summary, ensure_ascii=False)) <= 1200
    if value_width == 10:
        assert summary == arguments


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_argument_summary_normalizes_non_finite_floats_to_strict_json(value: float) -> None:
    summary = summarize_arguments({"value": value})

    assert isinstance(summary["value"], str)
    assert len(summary["value"]) <= 160
    encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    assert json.loads(encoded) == summary


def test_argument_summary_bounds_a_1000_layer_json_value() -> None:
    previous_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(previous_limit, 5000))
        value = json.loads("[" * 1000 + "0" + "]" * 1000)
    finally:
        sys.setrecursionlimit(previous_limit)

    summary = summarize_arguments(value)

    encoded = _assert_bounded_strict_json(summary)
    assert "[已达深度上限]" in encoded


@pytest.mark.parametrize("sign", [1, -1])
def test_argument_summary_normalizes_oversized_integers(sign: int) -> None:
    value = sign * 10**5000
    summary = summarize_arguments({"value": value})

    assert summary["value"] == "[整数过大]"
    _assert_bounded_strict_json(summary)


@pytest.mark.parametrize("container_kind", ["list", "dict"])
def test_argument_summary_terminates_for_self_referential_containers(
    container_kind: str,
) -> None:
    if container_kind == "list":
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)
        value: object = cyclic_list
    else:
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict
        value = cyclic_dict

    summary = summarize_arguments(value)

    encoded = _assert_bounded_strict_json(summary)
    assert "[循环引用]" in encoded
