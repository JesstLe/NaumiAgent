"""Manual Library calls retain exact identity and use the real authorization path."""

from __future__ import annotations

import json
import shlex

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal.artifact_commands import parse_library_command, run_library_command
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolResult

ID = "art_" + "a" * 32
GRANT = "asg_" + "b" * 32
ACTOR = "pa_" + "c" * 32
SHA = "d" * 64


def command(operation, fields):
    return operation + "-json " + shlex.quote(json.dumps(fields, ensure_ascii=False))


def version(**changes):
    return {
        "artifact_id": ID,
        "version": 3,
        "owner_id": "main",
        "title": "用户自定义创作导演",
        "sha256": SHA,
        "bytes": 25,
        "suffix": ".csv",
        "mime_type": "text/csv",
        "source_refs": [],
        "grant_id": "",
        "grant_revision": 0,
        "evidence": {},
        **changes,
    }


def grant(**changes):
    return {
        "grant_id": GRANT,
        "artifact_id": ID,
        "version": 3,
        "revision": 1,
        "recipient_id": ACTOR,
        "status": "active",
        "revoked_at": "",
        **changes,
    }


class EngineFacade:
    def __init__(self, payload, *, status="success"):
        self.calls = []
        self.content = (
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        )
        self.status = status

    @property
    def personal_artifacts(self):
        raise AssertionError("Commands must not bypass execute_tool and call the service")

    @property
    def tool_registry(self):
        raise AssertionError("Commands must not directly execute registry tools")

    async def execute_tool(self, call, *, agent_name=None):
        self.calls.append((call, agent_name))
        return ToolResult(call.id, self.status, self.content)


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", {"operation": "list"}),
        ("list", {"operation": "list"}),
        (
            "list material 月度 预算",
            {"operation": "list", "kind": "material", "query": "月度 预算"},
        ),
        (
            'list output "标题 with spaces"',
            {"operation": "list", "kind": "output", "query": "标题 with spaces"},
        ),
        ("list 预算", {"operation": "list", "query": "预算"}),
        ("get " + ID, {"operation": "get", "artifact_id": ID}),
        ("get " + ID + " 3", {"operation": "get", "artifact_id": ID, "version": 3}),
        ("versions " + ID, {"operation": "versions", "artifact_id": ID}),
        ("grants " + ID + " 3", {"operation": "grants", "artifact_id": ID, "version": 3}),
        ("read " + ID + " 3", {"operation": "read", "artifact_id": ID, "version": 3}),
        (
            "read " + ID + " 3 0",
            {"operation": "read", "artifact_id": ID, "version": 3, "offset": 0},
        ),
        (
            "read " + ID + " 3 25 20000",
            {"operation": "read", "artifact_id": ID, "version": 3, "offset": 25, "limit": 20000},
        ),
    ],
)
def test_positional_commands_bind_only_the_requested_operation_and_version(argument, expected):
    assert parse_library_command(argument) == expected


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        (
            "publish",
            {
                "request_id": "original-publish",
                "title": "导演's page",
                "filename": "draft.md",
                "content": "第一行\n第二行",
            },
        ),
        (
            "import",
            {"request_id": "original-import", "title": "文件", "path": "C:\\work\\真实.csv"},
        ),
        (
            "revise",
            {
                "request_id": "original-revise",
                "artifact_id": ID,
                "expected_version": 3,
                "title": "修订",
                "filename": "new.md",
                "content": "正文",
            },
        ),
        (
            "revise",
            {
                "request_id": "original-path-revise",
                "artifact_id": ID,
                "expected_version": 3,
                "title": "修订",
                "path": "budget.csv",
            },
        ),
        (
            "share",
            {
                "request_id": "original-share",
                "artifact_id": ID,
                "version": 3,
                "recipient_id": ACTOR,
            },
        ),
        ("revoke", {"request_id": "original-revoke", "grant_id": GRANT, "expected_revision": 1}),
    ],
)
async def test_each_json_operation_preserves_request_id_and_uses_authorized_engine_facade(
    action, fields
):
    payload = {action: grant()} if action in {"share", "revoke"} else {"artifact": version()}
    engine = EngineFacade(payload)
    argument = command(action, fields)
    expected = {"operation": action, **fields}
    assert parse_library_command(argument) == expected
    await run_library_command(engine, argument)
    await run_library_command(engine, argument)
    assert len(engine.calls) == 2
    assert engine.calls[0][0].id != engine.calls[1][0].id
    for call, actor in engine.calls:
        assert call.name == "personal_artifacts" and actor == "cli"
        assert json.loads(call.arguments) == expected


@pytest.mark.parametrize(
    "argument",
    [
        "get",
        "versions",
        "grants " + ID,
        "read " + ID,
        "get ../private 1",
        "get " + ID + " 0",
        "get " + ID + " 10001",
        "get " + ID + " ３",
        "get " + ID + " +1",
        "get " + ID + " 1.0",
        "get " + ID + " 1 extra",
        "read " + ID + " 1 -1",
        "read " + ID + " 1 0 0",
        "read " + ID + " 1 0 20001",
        "read " + ID + " 1 0 1 extra",
        "publish-json",
        "publish-json '{}'",
        "publish-json '[]'",
        "publish-json '{broken}'",
        "publish-json 'null'",
        "share-json '{}' extra",
        "unknown-json '{}'",
        "help extra",
        "get 'unterminated",
        "list " + "x" * 201,
    ],
)
async def test_invalid_shapes_do_not_dispatch(argument):
    engine = EngineFacade({})
    with pytest.raises(ValueError):
        parse_library_command(argument)
    result = await run_library_command(engine, argument)
    assert result and not engine.calls


@pytest.mark.parametrize(
    "key",
    [
        "actor_id",
        "owner_id",
        "owner",
        "session_id",
        "source_session_id",
        "operation",
        "evidence",
        "workspace_key",
    ],
)
async def test_manual_json_cannot_claim_another_actor_session_or_execution_evidence(key):
    fields = {
        "request_id": "stable",
        "title": "资料",
        "filename": "one.md",
        "content": "825",
        key: "forged",
    }
    engine = EngineFacade({})
    result = await run_library_command(engine, command("publish", fields))
    assert "未知字段" in result and not engine.calls


@pytest.mark.parametrize("action", ["publish", "import", "revise", "share", "revoke"])
async def test_mutating_commands_require_original_request_id_without_minting_a_new_one(action):
    engine = EngineFacade({})
    result = await run_library_command(engine, command(action, {}))
    assert "稳定的 request_id" in result and not engine.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"request_id":"one","request_id":"two"}',
        '{"request_id":"one","content":{"same":1,"same":2}}',
        '{"request_id":"one","content":NaN}',
        '{"request_id":"one","content":Infinity}',
        '{"request_id":"one","content":1e999}',
    ],
)
async def test_nonstandard_or_duplicate_json_is_rejected_before_dispatch(raw):
    engine = EngineFacade({})
    assert await run_library_command(engine, "publish-json " + shlex.quote(raw))
    assert not engine.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": ""},
        {"request_id": None},
        {"request_id": "new id"},
        {"content": ""},
        {"content": []},
        {"kind": {}},
        {"kind": "public"},
        {
            "source_refs": [
                {
                    "artifact_id": ID,
                    "version": 1,
                    "sha256": SHA,
                    "grant_id": "",
                    "grant_revision": 0,
                    "actor_id": "forged",
                }
            ]
        },
    ],
)
async def test_json_fields_are_checked_without_leaking_bad_calls_to_tools(changes):
    fields = {
        "request_id": "stable",
        "title": "资料",
        "filename": "one.md",
        "content": "825",
        **changes,
    }
    engine = EngineFacade({})
    assert await run_library_command(engine, command("publish", fields))
    assert not engine.calls


async def test_help_is_local_and_does_not_trigger_a_storage_or_model_action():
    engine = EngineFacade({})
    for argument in ("help", "-h", "--help"):
        result = await run_library_command(engine, argument)
        assert "request_id" in result and "/library read" in result and "Agent" in result
    assert not engine.calls


async def test_read_renders_the_entire_returned_chunk_without_hiding_pagination():
    content = "第一行 it's ready\n" + "实际正文" * 4000 + "\nLAST_VISIBLE_MARKER"
    engine = EngineFacade(
        {
            "artifact": version(grant_id=GRANT, grant_revision=1),
            "content": content,
            "offset": 12,
            "total_chars": len(content) + 50,
            "next_offset": len(content) + 12,
        }
    )
    result = await run_library_command(engine, "read " + ID + " 3 12 20000")
    assert content in result and result.endswith("下一段偏移：" + str(len(content) + 12))
    for field in (ID, SHA, GRANT, "v3", "r1", "偏移 12", "总计"):
        assert field in result


async def test_binary_read_uses_actual_no_text_receipt_instead_of_fabricating_body():
    engine = EngineFacade(
        {
            "artifact": version(suffix=".pdf", mime_type="application/pdf"),
            "text_available": False,
            "message": "请在资料页预览原文件",
        }
    )
    result = await run_library_command(engine, "read " + ID + " 3")
    assert "请在资料页预览原文件" in result and "正文：" not in result


@pytest.mark.parametrize(
    ("status", "content"),
    [
        ("error", "原授权已撤销，请核对原记录。"),
        ("unknown", "写入回应尚未取得，原请求结果待核对。"),
        ("success", {"status": "error", "detail": "原版本已更新，请刷新后重试"}),
        (
            "success",
            {
                "status": "unknown",
                "detail": {"code": "response_missing", "accepted": True, "artifact_id": ID},
            },
        ),
    ],
)
async def test_failure_and_unknown_keep_real_detail_without_claiming_non_admission(status, content):
    engine = EngineFacade(content, status=status)
    fields = {
        "request_id": "original-request",
        "title": "资料",
        "filename": "one.md",
        "content": "825",
    }
    result = await run_library_command(engine, command("publish", fields))
    assert "未受理" not in result and "已完成" not in result and "发布成功" not in result
    if isinstance(content, str):
        assert content in result and "original-request" in result
    else:
        detail = content["detail"]
        assert (detail if isinstance(detail, str) else "response_missing") in result


async def test_share_replay_displays_retracted_original_grant_not_success():
    original = grant(revision=2, status="revoked", revoked_at="2026-11-04T06:00:00Z")
    engine = EngineFacade({"share": original})
    result = await run_library_command(
        engine,
        command(
            "share",
            {
                "request_id": "original-share",
                "artifact_id": ID,
                "version": 3,
                "recipient_id": ACTOR,
            },
        ),
    )
    assert "已撤销" in result and "r2" in result and GRANT in result
    assert "成功" not in result and "已授权" not in result


@pytest.mark.parametrize(
    "payload", ["not json", [], {"get": {"artifact_id": ID}}, {"get": version(sha256="fake")}]
)
async def test_malformed_receipt_is_not_a_success_claim(payload):
    engine = EngineFacade(payload)
    result = await run_library_command(engine, "get " + ID)
    assert "回执格式有误" in result and "原回执" in result


@pytest.fixture
async def real_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "runtime-home"))

    async def no_provider(*args, **kwargs):
        raise AssertionError("Library commands must not contact any model")

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    work = tmp_path / "work"
    work.mkdir()
    state = tmp_path / "state"
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(work),
            personal={"data_dir": str(state / "personal")},
            memory={
                "session_db_path": str(state / "sessions.db"),
                "vector_db_path": str(state / "vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "bypass", "allowed_dirs": [str(work)]},
        )
    )
    await engine.get_or_create_session()
    try:
        yield engine, work
    finally:
        await engine.shutdown()


@pytest.mark.usefixtures("runtime_payload_key")
async def test_real_engine_tool_publish_replay_versions_import_and_full_read(real_engine):
    engine, work = real_engine
    original = {
        "request_id": "real-manual-publish",
        "title": "真实中文正文",
        "filename": "report.md",
        "content": "825 元\n真实完整第二行",
    }
    result = await run_library_command(engine, command("publish", original))
    values = await engine.personal_artifacts.list(actor_id="main")
    assert len(values) == 1 and values[0]["artifact_id"] in result and values[0]["sha256"] in result
    identity = values[0]["artifact_id"]
    assert await run_library_command(engine, command("publish", original)) == result
    assert len(await engine.personal_artifacts.list(actor_id="main")) == 1
    assert "真实完整第二行" in await run_library_command(engine, f"read {identity} 1")
    revised = {
        "request_id": "real-manual-revision",
        "title": "修订版本",
        "artifact_id": identity,
        "expected_version": 1,
        "filename": "report.md",
        "content": "第二版真实正文",
    }
    assert "v2" in await run_library_command(engine, command("revise", revised))
    history = await run_library_command(engine, "versions " + identity)
    assert "v1" in history and "v2" in history
    assert "真实完整第二行" in await run_library_command(engine, f"read {identity} 1")
    source = work / "source.csv"
    source.write_text("收入,支出\n1200,375\n", "utf-8")
    imported = {"request_id": "real-manual-import", "title": "实际CSV", "path": str(source)}
    assert "实际CSV" in await run_library_command(engine, command("import", imported))
    assert len(await engine.personal_artifacts.list(actor_id="main")) == 2


@pytest.mark.usefixtures("runtime_payload_key")
async def test_real_current_actor_and_revocation_cannot_be_bypassed_by_manual_cli_scope(
    real_engine,
):
    engine, _ = real_engine
    original_session = engine._session.id
    await run_library_command(
        engine,
        command(
            "publish",
            {
                "request_id": "real-original",
                "title": "确切原资料",
                "filename": "data.txt",
                "content": "PRIVATE_REAL_825",
            },
        ),
    )
    value = (await engine.personal_artifacts.list(actor_id="main"))[0]
    recipient = engine.personal_agents.create(
        name="用户定义角色",
        instructions="仅使用明确共享的资料",
        request_id="manual-recipient",
        tool_names=[],
        model_tier="fast",
    )
    daily = await engine.personal_agents.open_session(recipient.id)
    await engine.load_session(daily["session_id"])
    denied = await run_library_command(engine, f"read {value['artifact_id']} 1")
    assert "PRIVATE_REAL_825" not in denied and "操作未完成" in denied
    await engine.load_session(original_session)
    shared = await run_library_command(
        engine,
        command(
            "share",
            {
                "request_id": "real-share",
                "artifact_id": value["artifact_id"],
                "version": 1,
                "recipient_id": recipient.id,
            },
        ),
    )
    grants = await engine.personal_artifacts.grants(
        actor_id="main", artifact_id=value["artifact_id"], version=1
    )
    assert len(grants) == 1 and grants[0]["grant_id"] in shared
    await engine.load_session(daily["session_id"])
    assert "PRIVATE_REAL_825" in await run_library_command(engine, f"read {value['artifact_id']} 1")
    await engine.load_session(original_session)
    assert "已撤销" in await run_library_command(
        engine,
        command(
            "revoke",
            {
                "request_id": "real-revoke",
                "grant_id": grants[0]["grant_id"],
                "expected_revision": 1,
            },
        ),
    )
    await engine.load_session(daily["session_id"])
    denied = await run_library_command(engine, f"read {value['artifact_id']} 1")
    assert "PRIVATE_REAL_825" not in denied and "操作未完成" in denied
