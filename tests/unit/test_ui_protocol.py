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
