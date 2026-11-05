"""Exported personal-tool types preserve exact preexisting input and authority contracts."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from naumi_agent.personal.connection_tools import PersonalActionsTool, PersonalConnectionsTool
from naumi_agent.personal.item_tools import (
    FIELDS,
    REQUIRED,
    PersonalItemsTool,
    validate_item_arguments,
)
from naumi_agent.personal.models import PersonalAgentError

SchemaPath = tuple[str | int, ...]
ITEM = "pi_" + "a" * 32
OCCURRENCE = "pio_" + "b" * 32
CONNECTION = "pc_" + "c" * 32
ACTION = "ca_" + "d" * 32
ITEM_TYPE_PATHS: tuple[SchemaPath, ...] = (
    ("properties", "operation"),
    ("properties", "state"),
    ("properties", "schedule", "properties", "mode"),
    ("properties", "schedule", "properties", "misfire_policy"),
    ("properties", "policy", "properties", "tool_names", "items"),
    ("properties", "policy", "properties", "artifact_operations", "items"),
    ("properties", "policy", "properties", "model_tier"),
)
FOLD_PATH = ("properties", "schedule", "properties", "fold")
INVALID_VALUES = (None, False, True, 0, 1, 1.5, [], {}, "not-an-operation")
TOOL_TYPES = (PersonalItemsTool, PersonalConnectionsTool, PersonalActionsTool)


def _tool(tool_type):
    return tool_type(SimpleNamespace(engine=SimpleNamespace()))


def _schema_nodes(value: Any, path: SchemaPath = ()):
    """Walk schema-bearing positions, not arbitrary enum/default/example JSON values."""
    if not isinstance(value, dict):
        return
    yield path, value
    for keyword in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        for name, child in value.get(keyword, {}).items():
            yield from _schema_nodes(child, (*path, keyword, name))
    for keyword in ("oneOf", "anyOf", "allOf", "prefixItems"):
        for index, child in enumerate(value.get(keyword, [])):
            yield from _schema_nodes(child, (*path, keyword, index))
    for keyword in (
        "items", "additionalProperties", "contains", "not", "if", "then", "else",
        "propertyNames", "unevaluatedProperties",
    ):
        if keyword in value:
            yield from _schema_nodes(value[keyword], (*path, keyword))


def _node_at(schema: dict, path: SchemaPath) -> dict:
    value = schema
    for key in path:
        value = value[key]
    return value


def _changed_paths(tool_type, schema: dict) -> tuple[SchemaPath, ...]:
    if tool_type is PersonalItemsTool:
        return ITEM_TYPE_PATHS
    return tuple(("oneOf", index, "properties", "operation")
                 for index in range(len(schema["oneOf"])))


def _baseline_schema(tool_type, schema: dict) -> dict:
    original = copy.deepcopy(schema)
    for path in _changed_paths(tool_type, schema):
        _node_at(original, path).pop("type", None)
    return original


def _schedule() -> dict:
    return {
        "mode": "once", "timezone": "Asia/Shanghai", "local_time": "2026-11-07T09:00:00",
        "fold": None, "misfire_policy": "run_once", "grace_seconds": 3600,
    }


def _item_request(operation: str) -> dict:
    definition = {
        "request_id": "provider-schema-original-request", "title": "核对资料",
        "instructions": "只读取原版本并保存核对结果", "assignee_id": "main",
        "assignee_version": 0, "schedule": _schedule(), "tags": [], "directories": [],
        "input_refs": [], "policy": {
            "tool_names": ["file_read", "personal_artifacts"],
            "artifact_operations": ["read", "publish"], "model_tier": "capable",
            "max_turns": 4, "max_cost_microusd": 1000, "timeout_seconds": 60,
        },
    }
    values = {
        **definition, "item_id": ITEM, "occurrence_id": OCCURRENCE, "expected_revision": 1,
        "state": "active", "query": "资料", "limit": 20, "offset": 0, "unread": True,
    }
    return {"operation": operation, **{key: values[key] for key in FIELDS[operation]}}


def _calendar_request(operation: str) -> dict:
    mutation = {"request_id": "calendar-original-request", "expected_revision": 1}
    if operation in {"context", "list", "list_actions"}:
        return {"operation": operation}
    if operation == "events":
        return {"operation": operation, "connection_id": CONNECTION, "limit": 20}
    if operation == "sync":
        return {"operation": operation, "connection_id": CONNECTION, "payload": mutation}
    if operation == "event_to_item":
        return {
            "operation": operation, "connection_id": CONNECTION, "event_id": "original-event",
            "payload": {
                "request_id": "event-original-request", "event_revision": 1,
                "title": "跟进日程", "instructions": "生成待处理事项", "schedule": _schedule(),
            },
        }
    if operation == "get_action":
        return {"operation": operation, "action_id": ACTION}
    if operation == "reconcile":
        return {"operation": operation, "action_id": ACTION, "payload": mutation}
    assert operation == "prepare_action"
    return {
        "operation": operation,
        "payload": {
            "request_id": "action-original-request", "connection_id": CONNECTION,
            "source_refs": [{
                "connection_id": CONNECTION, "auth_epoch": 1, "event_id": "original-event",
                "revision": 1, "payload_sha256": "e" * 64, "occurrence_key": "original-key",
                "grant_id": "", "grant_revision": 0,
            }],
            "operation": "insert", "body": {
                "summary": "待批准日程", "description": "保留人工审批",
                "start": {"date": "2026-11-07"}, "end": {"date": "2026-11-08"},
            },
            "expires_at": "2026-11-07T01:00:00Z",
        },
    }


def _requests(tool_type) -> list[dict]:
    if tool_type is PersonalItemsTool:
        return [_item_request(operation) for operation in FIELDS]
    schema = _tool(tool_type).parameters_schema
    return [_calendar_request(branch["properties"]["operation"]["const"])
            for branch in schema["oneOf"]]


@pytest.mark.parametrize("tool_type", TOOL_TYPES)
def test_exported_enum_and_const_schema_nodes_have_explicit_types(tool_type) -> None:
    tool = _tool(tool_type)
    exported = tool.to_openai_tool()
    schema = exported["function"]["parameters"]
    assert exported["type"] == "function"
    assert exported["function"]["name"] == tool.name
    assert schema == tool.parameters_schema
    Draft202012Validator.check_schema(schema)
    nodes = [(path, node) for path, node in _schema_nodes(schema)
             if "enum" in node or "const" in node]
    assert nodes
    for path, node in nodes:
        if tool_type is PersonalItemsTool and path == FOLD_PATH:
            assert node == {"type": ["integer", "null"], "enum": [0, 1, None]}
        else:
            assert node.get("type") == "string", path
            assert all(isinstance(value, str) for value in node.get("enum", [node.get("const")]))


@pytest.mark.parametrize("path", ITEM_TYPE_PATHS)
def test_each_changed_item_enum_preserves_exact_leaf_acceptance(path: SchemaPath) -> None:
    schema = _tool(PersonalItemsTool).parameters_schema
    current = _node_at(schema, path)
    original = copy.deepcopy(current)
    original.pop("type", None)
    assert current.get("type") == "string"
    before, after = Draft202012Validator(original), Draft202012Validator(current)
    for value in [*current["enum"], *INVALID_VALUES]:
        expected = isinstance(value, str) and value in current["enum"]
        assert before.is_valid(value) is expected
        assert after.is_valid(value) is expected


@pytest.mark.parametrize("tool_type", (PersonalConnectionsTool, PersonalActionsTool))
def test_each_calendar_operation_const_preserves_exact_leaf_acceptance(tool_type) -> None:
    schema = _tool(tool_type).parameters_schema
    for path in _changed_paths(tool_type, schema):
        current = _node_at(schema, path)
        original = copy.deepcopy(current)
        original.pop("type", None)
        assert current.get("type") == "string"
        before, after = Draft202012Validator(original), Draft202012Validator(current)
        for value in [current["const"], *INVALID_VALUES]:
            expected = isinstance(value, str) and value == current["const"]
            assert before.is_valid(value) is expected
            assert after.is_valid(value) is expected


@pytest.mark.parametrize("tool_type", TOOL_TYPES)
def test_complete_operation_payloads_preserve_schema_acceptance_and_unknown_field_rejection(
    tool_type,
) -> None:
    schema = _tool(tool_type).parameters_schema
    before = Draft202012Validator(_baseline_schema(tool_type, schema))
    after = Draft202012Validator(schema)
    for request in _requests(tool_type):
        assert before.is_valid(request), request
        assert after.is_valid(request), request
        if tool_type is PersonalItemsTool:
            assert validate_item_arguments(copy.deepcopy(request)) == request
            assert REQUIRED[request["operation"]] <= request.keys()
        for field in ("caller_actor", "actor_id", "trusted_channel", "origin_session_id",
                      "human", "approved", "expected_digest", "unknown"):
            invalid = {**request, field: "forged"}
            assert not before.is_valid(invalid), (request["operation"], field)
            assert not after.is_valid(invalid), (request["operation"], field)
        for operation in INVALID_VALUES:
            invalid = {**request, "operation": operation}
            assert not before.is_valid(invalid)
            assert not after.is_valid(invalid)
        assert not before.is_valid({})
        assert not after.is_valid({})


@pytest.mark.parametrize("operation", tuple(FIELDS))
def test_item_runtime_field_and_required_contract_is_not_broadened(operation: str) -> None:
    request = _item_request(operation)
    for field in REQUIRED[operation]:
        invalid = {key: value for key, value in request.items() if key != field}
        with pytest.raises(PersonalAgentError) as error:
            validate_item_arguments(invalid)
        assert error.value.code == "invalid_input"
    disallowed = next(name for name in ("item_id", "occurrence_id", "title", "state")
                      if name not in FIELDS[operation])
    with pytest.raises(PersonalAgentError) as error:
        validate_item_arguments({**request, disallowed: "not-permitted-for-operation"})
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("tool_type", TOOL_TYPES)
def test_nested_enum_changes_preserve_full_request_acceptance(tool_type) -> None:
    schema = _tool(tool_type).parameters_schema
    before = Draft202012Validator(_baseline_schema(tool_type, schema))
    after = Draft202012Validator(schema)
    if tool_type is PersonalItemsTool:
        request = _item_request("create")
        fields = (
            (("schedule", "mode"), ["now", "once", *INVALID_VALUES]),
            (("schedule", "misfire_policy"), ["run_once", "skip", *INVALID_VALUES]),
            (("policy", "model_tier"), ["fast", "capable", "reasoning", *INVALID_VALUES]),
            (("policy", "tool_names"), [["file_read"], ["read"], ["unknown"], [1], None]),
            (("policy", "artifact_operations"), [["get"], ["read", "publish"], [1], None]),
        )
        for path, values in fields:
            for value in values:
                changed = copy.deepcopy(request)
                changed[path[0]][path[1]] = value
                assert before.is_valid(changed) == after.is_valid(changed), (path, value)
    else:
        for request in _requests(tool_type):
            if "payload" not in request:
                continue
            for field in ("actor_id", "caller_actor", "approved", "human", "expected_digest"):
                changed = copy.deepcopy(request)
                changed["payload"][field] = "forged"
                assert not before.is_valid(changed)
                assert not after.is_valid(changed)


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_action_proposal_operation_does_not_acquire_approval_or_execution(operation: str) -> None:
    schema = _tool(PersonalActionsTool).parameters_schema
    before = Draft202012Validator(_baseline_schema(PersonalActionsTool, schema))
    after = Draft202012Validator(schema)
    request = _calendar_request("prepare_action")
    request["payload"]["operation"] = operation
    if operation == "update":
        request["payload"].update(target_event_id="original-event", base_etag='"etag-original"')
    assert before.is_valid(request) and after.is_valid(request)
    for privileged_operation in ("approve_action", "reject_action", "execute_action", "disconnect",
                                 "grant", "oauth", "read_result"):
        invalid = {**request, "operation": privileged_operation}
        assert not before.is_valid(invalid)
        assert not after.is_valid(invalid)


def test_fold_union_and_lifecycle_operation_sets_are_preserved() -> None:
    tool = _tool(PersonalItemsTool)
    schema = tool.parameters_schema
    assert _node_at(schema, FOLD_PATH) == {"type": ["integer", "null"], "enum": [0, 1, None]}
    assert schema["properties"]["operation"]["enum"] == list(FIELDS)
    assert schema["properties"]["state"]["enum"] == ["active", "paused", "cancelled", "closed"]
    assert tool.metadata.requires_confirmation is True
    for value in (0, 1, None):
        assert Draft202012Validator(_node_at(schema, FOLD_PATH)).is_valid(value)
    for value in (True, False, 2, "0", [], {}):
        assert not Draft202012Validator(_node_at(schema, FOLD_PATH)).is_valid(value)


def test_schema_walker_visits_definitions_and_branches_not_data_values() -> None:
    schema = {
        "type": "object", "default": {"enum": ["data"]},
        "examples": [{"const": "data"}], "properties": {"enum": {"type": "string"}},
        "$defs": {"Choice": {"type": "string", "enum": ["real"]}},
        "oneOf": [{"const": "actual", "type": "string"}],
    }
    paths = {path for path, _ in _schema_nodes(schema)}
    assert paths == {(), ("properties", "enum"), ("$defs", "Choice"), ("oneOf", 0)}
