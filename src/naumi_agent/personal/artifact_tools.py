"""Strict manual/agent calls into the same artifact broker."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from jsonschema import Draft202012Validator

from naumi_agent.personal.artifact_models import REF_FIELDS, is_text_artifact, strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata
from naumi_agent.tools.output_publish import MAX_ASSET_BYTES

_FIELDS = {
    "list": {"kind", "query", "limit", "offset"},
    "get": {"artifact_id", "version"},
    "read": {"artifact_id", "version", "offset", "limit"},
    "versions": {"artifact_id"},
    "publish": {"request_id", "title", "kind", "filename", "content", "source_refs"},
    "import": {"request_id", "title", "kind", "path", "source_refs"},
    "revise": {
        "request_id",
        "artifact_id",
        "expected_version",
        "title",
        "kind",
        "filename",
        "content",
        "path",
        "source_refs",
    },
    "share": {"artifact_id", "version", "recipient_id", "request_id"},
    "grants": {"artifact_id", "version"},
    "revoke": {"grant_id", "expected_revision", "request_id"},
}
_REQUIRED = {
    "list": set(),
    "get": {"artifact_id"},
    "read": {"artifact_id", "version"},
    "versions": {"artifact_id"},
    "publish": {"request_id", "title", "filename", "content"},
    "import": {"request_id", "title", "path"},
    "revise": {"request_id", "artifact_id", "expected_version", "title"},
    "share": {"artifact_id", "version", "recipient_id", "request_id"},
    "grants": {"artifact_id", "version"},
    "revoke": {"grant_id", "expected_revision", "request_id"},
}


class PersonalArtifactsTool(Tool):
    name = "personal_artifacts"
    description = (
        "管理当前 Agent 自己的持久资料库。list/get/read/versions查询确切版本；"
        "publish以真实UTF-8正文或Page JSON创建产物，import导入已存在的工作区文件；"
        "revise绑定expected_version生成新版本，不覆盖旧引用；share显式分享具体版本给已有 Agent，"
        "revoke以grant_id和expected_revision撤销。任何操作不接受伪造actor/owner/session。"
        "read的授权在后续模型和工具边界持续复核；私有内容计算只在无网络隔离环境执行，"
        "文件写入/outputs后由broker保存为真实产物，不向共享工作区或公网自动导出。"
        "Page JSON为version=1,type=page,title,sections数组；每节id/title/evidence_status"
        "(fact/estimate/unknown)，markdown或widget，source_refs保存exact版本/sha/grant。"
    )

    def __init__(self, engine):
        self.engine = engine
        self.service = engine.personal_artifacts

    def parse_arguments(self, raw):
        if isinstance(raw, str):
            try:
                raw = strict_json_loads(raw)
            except PersonalAgentError as exc:
                raise ValueError(exc.message) from exc
        return super().parse_arguments(raw)

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=True,
            user_facing_name="资料与产物",
            search_hint="library artifact version page document share revoke material",
        )

    @property
    def parameters_schema(self):
        properties = {
            "operation": {"type": "string", "enum": list(_FIELDS)},
            "artifact_id": {"type": "string", "pattern": "^art_[a-f0-9]{32}$"},
            "version": {"type": "integer", "minimum": 1, "maximum": 10000},
            "expected_version": {"type": "integer", "minimum": 1, "maximum": 10000},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "kind": {"type": "string", "enum": ["material", "output"]},
            "filename": {"type": "string", "minLength": 1, "maxLength": 240},
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "content": {"type": "string", "minLength": 1, "maxLength": MAX_ASSET_BYTES},
            "source_refs": {"type": "array", "maxItems": 64, "items": {"type": "object"}},
            "recipient_id": {"type": "string", "pattern": "^(main|pa_[0-9a-f]{32})$"},
            "grant_id": {"type": "string", "pattern": "^asg_[0-9a-f]{32}$"},
            "expected_revision": {"type": "integer", "minimum": 1, "maximum": 2},
            "query": {"type": "string", "maxLength": 200},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000},
        }
        return {
            "type": "object",
            "properties": properties,
            "required": ["operation"],
            "additionalProperties": False,
        }

    async def execute(self, operation: str, **raw):
        try:
            value = {"operation": operation, **raw}
            if (
                list(Draft202012Validator(self.parameters_schema).iter_errors(value))
                or operation not in _FIELDS
                or set(raw) - _FIELDS[operation]
                or not _REQUIRED[operation] <= set(raw)
            ):
                raise PersonalAgentError("invalid_input", "资料操作包含无效字段或缺少必要参数")
            actor = self.service.engine_actor()
            fields = dict(raw)
            session = self.engine._session
            if operation == "list":
                return json.dumps(
                    {"artifacts": await self.service.list(actor_id=actor, **fields)},
                    ensure_ascii=False,
                )
            if operation in {"get", "versions", "grants", "share", "revoke"}:
                result = await getattr(self.service, operation)(actor_id=actor, **fields)
                return json.dumps({operation: result}, ensure_ascii=False)
            if operation == "read":
                offset, limit = fields.pop("offset", 0), fields.pop("limit", 12000)
                snapshot, content = await self.service.read(actor_id=actor, **fields)
                await self.service.bind_refs(session.id, actor, [self.service.reference(snapshot)])
                if not is_text_artifact(snapshot):
                    return json.dumps(
                        {
                            "artifact": snapshot,
                            "text_available": False,
                            "input_path": (
                                f"/inputs/{snapshot['artifact_id']}"
                                f"-v{snapshot['version']}{snapshot['suffix']}"
                            ),
                            "message": "请在资料页预览或使用具备图像/文档读取能力的工具",
                        },
                        ensure_ascii=False,
                    )
                self.engine._personal_artifact_refs = await self.service.session_refs(
                    session.id, actor
                )
                text = content.decode("utf-8-sig")
                end = min(len(text), offset + limit)
                return json.dumps(
                    {
                        "artifact": snapshot,
                        "content": text[offset:end],
                        "offset": offset,
                        "input_path": (
                            f"/inputs/{snapshot['artifact_id']}"
                            f"-v{snapshot['version']}{snapshot['suffix']}"
                        ),
                        "next_offset": end if end < len(text) else None,
                        "total_chars": len(text),
                    },
                    ensure_ascii=False,
                )
            if "path" in fields:
                if "content" in fields or "filename" in fields:
                    raise PersonalAgentError("invalid_input", "实际文件路径与正文只能选择一种")
                from naumi_agent.personal.artifact_boundary import checked_public_path

                path = checked_public_path(self.engine, fields.pop("path"))
                if not path.is_file():
                    raise PersonalAgentError("not_found", "待导入的工作区文件不存在")
                with path.open("rb") as stream:
                    content = stream.read(MAX_ASSET_BYTES + 1)
                fields["filename"] = path.name
            else:
                if "content" not in fields or "filename" not in fields:
                    raise PersonalAgentError(
                        "invalid_input", "修订需要正文与文件名，或实际文件路径"
                    )
                content = fields.pop("content").encode("utf-8")
            if operation == "revise":
                original = await self.service.get(
                    actor_id=actor,
                    artifact_id=fields["artifact_id"],
                    version=fields["expected_version"],
                )
                fields.setdefault("kind", original["kind"])
            else:
                fields.setdefault("kind", "material" if operation == "import" else "output")
            evidence = {"source_kind": "tool", "session_id": session.id, "tool_name": self.name}
            from naumi_agent.personal.runtime import current_run_id

            run_id = current_run_id(self.engine, session.id)
            if run_id:
                evidence["run_id"] = run_id
            result = await self.service.publish(
                actor_id=actor,
                content=content,
                evidence=evidence,
                session_id=session.id,
                _trusted_execution=True,
                **fields,
            )
            ref = {key: result[key] for key in REF_FIELDS}
            outputs = getattr(self.engine, "_personal_artifact_outputs", [])
            if ref not in outputs:
                outputs.append(ref)
            self.engine._personal_artifact_outputs = outputs
            url = "/web2?" + urlencode(
                {"artifact": result["artifact_id"], "version": result["version"], "actor": actor}
            )
            return json.dumps(
                {"artifact": result, "url": url, "markdown": f"[{result['title']}]({url})"},
                ensure_ascii=False,
            )
        except PersonalAgentError as exc:
            raise ToolExecutionError(exc.code, exc.message[:300]) from exc
