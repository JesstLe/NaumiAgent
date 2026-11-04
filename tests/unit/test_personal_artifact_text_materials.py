"""Private UTF-8 source material compatibility without public asset expansion."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from naumi_agent.personal.artifact_models import (
    PRIVATE_TEXT_MATERIAL_TYPES,
    is_text_artifact,
    validate_content,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolCall
from naumi_agent.tools.output_publish import (
    ASSET_NAME,
    ASSET_TYPES,
    MAX_ASSET_BYTES,
    validate_asset,
)
from tests.unit.test_personal_artifacts import artifacts as artifacts

pytestmark = pytest.mark.usefixtures("runtime_payload_key")

_SAMPLES = [
    (".py", "text/x-python", '# 预算\r\nprint("<script>source_only()</script>")\r\n'),
    (".ts", "text/typescript", '// 预算\nconst amount: number = 825;\n'),
    (".yaml", "application/yaml", '# 保留配置\namount: 825\nhtml: "<img onerror=source_only()>"\n'),
    (".log", "text/plain", '2026-11-04 INFO 结余=825\r\n<script>source_only()</script>\r\n'),
]


async def _execute(context, operation, **arguments):
    result = await context.engine.execute_tool(
        ToolCall(
            id="text-material-" + operation, name="personal_artifacts",
            arguments={"operation": operation, **arguments},
        )
    )
    assert result.status == "success", result
    return json.loads(result.content)


@pytest.mark.parametrize("suffix,mime,body", _SAMPLES)
async def test_source_material_upload_import_read_and_versions_keep_exact_bytes(
    artifacts, suffix, mime, body,
):
    context = artifacts
    actor, session_id = context.profiles[0].id, context.sessions[0]
    data = body.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    name = "source" + suffix
    uploaded = await context.client.post(
        f"/sessions/{session_id}/upload", files={"file": (name, data, "application/octet-stream")},
    )
    assert uploaded.status_code == 201, uploaded.text
    card = uploaded.json()
    assert card["kind"] == "artifact" and card["path"].startswith("artifact://")
    ref = card["metadata"]["artifact_ref"]
    assert ref["sha256"] == digest and ref["version"] == 1
    first = await context.service.get(actor_id=actor, artifact_id=ref["artifact_id"], version=1)
    imported = await context.client.post(
        "/personal/artifacts/import",
        params={"actor_id": actor, "request_id": "library-source", "kind": "material"},
        files={"file": (name, data, "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    workspace_file = context.workspace / name
    workspace_file.write_bytes(data)
    subprocess.run(
        ["git", "-C", str(context.workspace), "add", name], check=True, capture_output=True,
    )
    sourced = await context.client.post(
        f"/sessions/{session_id}/sources", json={"path": name, "title": "工作区源码"},
    )
    assert sourced.status_code == 201, sourced.text
    source_ref = sourced.json()["metadata"]["artifact_ref"]
    source_item = await context.service.get(
        actor_id=actor, artifact_id=source_ref["artifact_id"], version=1,
    )
    assert await context.engine.load_session(session_id)
    tool_import = await _execute(
        context, "import", path=name, title="工具导入源码", kind="material",
        request_id="tool-source",
    )
    values = [first, imported.json(), source_item, tool_import["artifact"]]
    for item in values:
        assert item["kind"] == "material" and item["owner_id"] == actor
        assert item["suffix"] == suffix and item["mime_type"] == mime
        assert item["sha256"] == digest and item["bytes"] == len(data) and item["version"] == 1
        assert is_text_artifact(item)
        read = await _execute(context, "read", artifact_id=item["artifact_id"], version=1)
        assert read["content"] == body and read.get("text_available") is not False
        assert read["input_path"].endswith("-v1" + suffix)
        preview = await context.client.get(
            f"/personal/artifacts/{item['artifact_id']}/preview",
            params={"actor_id": actor, "version": 1},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json() == {"type": "text", "content": body, "truncated": False}
        downloaded = await context.client.get(
            f"/personal/artifacts/{item['artifact_id']}/content",
            params={"actor_id": actor, "version": 1},
        )
        assert downloaded.status_code == 200 and downloaded.content == data
        assert downloaded.headers["X-Content-SHA256"] == digest
        assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in downloaded.headers["Content-Security-Policy"]
    sources = await context.engine.chat_run_store.list_sources(session_id)
    source = next(value for value in sources if value.id == card["id"])
    assert await context.service.source_text(session_id=session_id, source=source) == body
    revised = await context.service.publish(
        actor_id=actor, artifact_id=first["artifact_id"], expected_version=1,
        request_id="revised-source", title="新版本源码", filename=name, kind="material",
        content=data + b"\n# version 2\n",
    )
    assert revised["version"] == 2 and revised["sha256"] != digest
    old, old_bytes = await context.service.read(
        actor_id=actor, artifact_id=first["artifact_id"], version=1,
    )
    assert old["sha256"] == digest and old_bytes == data
    assert not (context.workspace / ".naumi/uploads").exists()
    assert await context.engine.chat_run_store.list_runs(session_id) == []


@pytest.mark.parametrize("suffix", [sample[0] for sample in _SAMPLES])
@pytest.mark.parametrize("bad", [b"\xff\xfe\x80", b"text\x00binary"])
async def test_invalid_utf8_or_binary_source_upload_is_rejected_without_artifact(
    artifacts, suffix, bad,
):
    context = artifacts
    response = await context.client.post(
        f"/sessions/{context.sessions[0]}/upload",
        files={"file": ("bad" + suffix, bad, "text/plain")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_input"
    assert await context.service.list(actor_id=context.profiles[0].id) == []
    assert await context.engine.chat_run_store.list_sources(context.sessions[0]) == []


@pytest.mark.parametrize("suffix", [".html", ".js", ".css"])
async def test_active_language_sources_are_only_inert_plain_text(artifacts, suffix):
    context = artifacts
    actor = context.profiles[0].id
    body = '<script>window.source_only = true</script>\n<img src=x onerror=source_only()>\n'
    response = await context.client.post(
        "/personal/artifacts/import",
        params={"actor_id": actor, "request_id": "inert-source", "kind": "material"},
        files={"file": ("source" + suffix, body.encode(), "text/html")},
    )
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["suffix"] == suffix and item["mime_type"] == "text/plain"
    for surface in ("preview", "content"):
        result = await context.client.get(
            f"/personal/artifacts/{item['artifact_id']}/{surface}",
            params={"actor_id": actor, "version": 1},
        )
        assert result.status_code == 200, result.text
        if surface == "preview":
            assert result.json() == {"type": "text", "content": body, "truncated": False}
        else:
            assert result.headers["Content-Type"].startswith("text/plain")
            assert result.text == body and result.headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'none'" in result.headers["Content-Security-Policy"]


@pytest.mark.parametrize("suffix", list(PRIVATE_TEXT_MATERIAL_TYPES))
def test_material_registry_never_expands_private_output_or_public_assets(suffix):
    content = "# source only 预算\n".encode()
    raw, normalized, mime = validate_content(content, suffix, kind="material")
    assert raw == content and normalized == suffix and mime == PRIVATE_TEXT_MATERIAL_TYPES[suffix]
    assert suffix not in ASSET_TYPES
    assert ASSET_NAME.fullmatch("a" * 64 + suffix) is None
    with pytest.raises(PersonalAgentError, match="支持 SVG"):
        validate_content(content, suffix, kind="output")
    with pytest.raises(ValueError, match="支持 SVG"):
        validate_asset(content, suffix)


@pytest.mark.parametrize(
    "content", [b"", b"x" * (MAX_ASSET_BYTES + 1)], ids=["empty", "over-limit"],
)
def test_private_text_material_size_limits_are_still_enforced(content):
    with pytest.raises(PersonalAgentError, match="20 MB"):
        validate_content(content, ".py", kind="material")


def test_utf8_bom_is_preserved_in_blob_and_uppercase_extension_is_normalized():
    raw = b"\xef\xbb\xbf# source\r\n"
    content, suffix, mime = validate_content(raw, ".PY", kind="material")
    assert content == raw and suffix == ".py" and mime == "text/x-python"


@pytest.mark.parametrize("suffix", [".exe", ".dll", ".wasm", ".bin"])
def test_unknown_binary_extensions_do_not_become_private_text(suffix):
    with pytest.raises(PersonalAgentError, match="支持 SVG"):
        validate_content(b"looks like utf8", suffix, kind="material")
