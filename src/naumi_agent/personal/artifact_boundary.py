"""Artifact-aware execution guard and fail-closed private process routing."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from urllib.parse import urlencode

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolExecutionError

_PUBLIC_WRITES = {"file_write", "file_edit", "output_publish"}
_NETWORK = {"web_fetch", "web_search"}


def protected_roots(engine):
    workspace = Path(engine.workspace_root).resolve()
    roots = [engine.personal_agents.store.base_dir.resolve(), workspace / ".naumi"]
    artifacts = getattr(engine, "personal_artifacts", None)
    if artifacts is not None:
        roots.append(artifacts.runtime_boundary_marker)
    connections = getattr(engine, "personal_connections", None)
    if connections is not None:
        roots.extend([connections.boundary, connections.store.base_dir.resolve()])
    memory = getattr(engine, "personal_memory", None)
    if memory is not None:
        roots.extend([memory.runtime_boundary_marker, memory.owner_boundary_marker,
                      memory.store.base_dir.resolve()])
    vector_path = getattr(engine.config.memory, "vector_db_path", "")
    if vector_path:
        roots.append(Path(vector_path).expanduser().resolve())
    runtime = Path(engine.config.memory.session_db_path).resolve().parent
    if runtime != workspace and not workspace.is_relative_to(runtime):
        roots.append(runtime)
    else:
        roots.extend(runtime.glob("*.db*"))
    return roots


def is_protected(engine, path):
    resolved = path.resolve()
    return any(
        resolved == root or resolved.is_relative_to(root) for root in protected_roots(engine)
    )


def checked_public_path(engine, raw):
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise PersonalAgentError("invalid_input", "文件路径格式不正确")
    root = Path(engine.workspace_root).resolve()
    candidate = Path(raw).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root) or is_protected(engine, path):
        raise PersonalAgentError(
            "artifact_path_denied", "该路径属于私有运行资料，请通过确切资料版本读取"
        )
    if path.exists() and (path.is_symlink() or getattr(path.stat(), "st_nlink", 1) > 1):
        raise PersonalAgentError(
            "artifact_path_denied", "资料路径存在链接别名，请选择独立的工作区文件"
        )
    return path


class ArtifactToolProxy:
    def __init__(self, engine, tool, call_id):
        self.engine, self.tool, self.call_id = engine, tool, call_id
        self.name = tool.name

    async def execute(self, *, event_callback=None, **args):
        service = self.engine.personal_artifacts
        try:
            await service.authorize_engine()
            memory_runtime = getattr(self.engine, "personal_memory_runtime", None)
            if memory_runtime is not None:
                await memory_runtime.authorize()
            item_context = getattr(self.engine, "_personal_item_context", None)
            if item_context is not None:
                await item_context.authorize_tool(self.name, args)
            refs = list({json.dumps(r, sort_keys=True): r for r in [
                *getattr(self.engine, "_personal_artifact_refs", []),
                *getattr(self.engine, "_personal_memory_artifact_refs", []),
            ]}.values())
            private = bool(refs) or bool(getattr(item_context, "force_private", False))
            actor = service.engine_actor()
            # The authenticated user's Library is distinct from agent browser
            # contexts. Host CDP can otherwise reach local management endpoints.
            if self.name.startswith("browser_"):
                raise PersonalAgentError(
                    "artifact_isolation_required",
                    "当前 Agent 的浏览器隔离入口尚未启动，已停止本次操作",
                )
            if private and (self.name in _PUBLIC_WRITES or self.name in _NETWORK):
                raise PersonalAgentError(
                    "artifact_export_required",
                    "本对话含私有资料，请先保存为产物，再由用户明确分享或导出",
                )
            if private and self.name not in {
                "personal_artifacts",
                "bash_run",
                "code_execute",
                "file_read",
                "read",
                "glob",
                "grep",
                "personal_team",
                "personal_message",
                "request_user_input",
                "personal_agents",
                "personal_items",
                "personal_memories",
                "personal_connections",
                "personal_actions",
                "memory_store",
                "memory_recall",
            }:
                raise PersonalAgentError(
                    "artifact_isolation_required", "该工具尚未接入私有资料执行边界，已停止本次操作"
                )
            if item_context is not None and self.name in {"file_read", "read"}:
                return await self._read_input(actor, refs, args)
            if self.name != "personal_artifacts" and item_context is None:
                path_names = set(self.tool.metadata.path_argument_names) | {
                    "path",
                    "cwd",
                    "file_path",
                    "directory",
                    "target_path",
                }
                for key in path_names:
                    if isinstance(args.get(key), str) and args[key]:
                        checked_public_path(self.engine, args[key])
            if private and self.name == "personal_message" and args.get("operation") == "send":
                for ref in refs:
                    await service.get(
                        actor_id=args["recipient_agent_id"],
                        artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    )
            if self.name in {"bash_run", "code_execute"}:
                return await self._process(actor, refs, args)
            if self.name in {"yaml_validate", "yaml_micro_verify"}:
                raise PersonalAgentError(
                    "artifact_isolation_required",
                    "该校验器尚未接入隔离计算，请使用已接入的代码执行工具",
                )
            target = self.tool
            if self.name == "grep":
                # Copy per invocation; do not mutate a shared registry tool while
                # another Engine/Task is scanning the same project.
                target = copy.copy(self.tool)
                original = self.tool._iter_search_files
                target._iter_search_files = lambda *a: [
                    p
                    for p in original(*a)
                    if not is_protected(self.engine, p) and getattr(p.stat(), "st_nlink", 1) <= 1
                ]
            if self.name == "glob":
                directory = checked_public_path(self.engine, args.get("directory", "."))
                pattern = args["pattern"]
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    raise PersonalAgentError("invalid_input", "查找模式需要工作区内相对路径")
                values = []
                for path in directory.glob(pattern):
                    if (
                        path.is_file()
                        and not is_protected(self.engine, path)
                        and path.resolve().is_relative_to(
                            Path(self.engine.workspace_root).resolve()
                        )
                        and getattr(path.stat(), "st_nlink", 1) <= 1
                    ):
                        values.append(
                            path.resolve()
                            .relative_to(Path(self.engine.workspace_root).resolve())
                            .as_posix()
                        )
                values = sorted(set(values))
                limit = min(500, max(1, int(args.get("limit", 100))))
                return json.dumps(
                    {"files": values[:limit], "total": len(values), "private_paths_excluded": True},
                    ensure_ascii=False,
                )
            if (
                event_callback is not None
                and "event_callback" in inspect.signature(target.execute).parameters
            ):
                args["event_callback"] = event_callback
            return await target.execute(**args)
        except PersonalAgentError as exc:
            raise ToolExecutionError(exc.code, exc.message[:300]) from exc

    async def _process(self, actor, refs, args):
        from naumi_agent.personal.artifact_process import execute_private_process

        service = self.engine.personal_artifacts
        session_id = self.engine._session.id
        from naumi_agent.personal.runtime import current_run_id

        run_id = current_run_id(self.engine, session_id)
        inputs = {}
        for ref in refs:
            snapshot, content = await service.read(
                actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            name = f"{snapshot['artifact_id']}-v{snapshot['version']}{snapshot['suffix']}"
            inputs[name] = content
        language = "shell" if self.name == "bash_run" else args.get("language", "python")
        language = "shell" if language == "bash" else language
        command = args.get("command") if self.name == "bash_run" else args.get("code")
        # Private calculations see a read-only, bounded public snapshot, not a
        # writable host project or runtime data. Results leave only via broker.
        try:
            item_context = getattr(self.engine, "_personal_item_context", None)
            result = await execute_private_process(
                workspace=Path(
                    item_context.process_workspace
                    if item_context is not None
                    else self.engine.workspace_root
                ),
                command=command,
                language=language,
                inputs=inputs,
                private=True,
                empty_workspace=item_context is not None,
                protected_roots=protected_roots(self.engine),
                image="node:24-bookworm-slim" if language == "javascript" else "python:3.12-slim",
                timeout_s=args.get("timeout", 30),
            )
        except PersonalAgentError as exc:
            await self._record_process(
                {"error_code": exc.code, "isolation": getattr(exc, "isolation", {})},
                "error",
                run_id=run_id,
            )
            raise
        await self._record_process(
            {key: result[key] for key in ("stdout", "stderr", "exit_code", "isolation")},
            "completed" if result["exit_code"] == 0 else "error",
            run_id=run_id,
        )
        await service.authorize_engine()
        if item_context is not None:
            await item_context.authorize()
        if self.engine._session.id != session_id or service.engine_actor() != actor:
            raise PersonalAgentError("binding_conflict", "隔离计算的执行身份已变化，结果未发布")
        if result["exit_code"] != 0:
            detail = str(result["stderr"] or "").strip().splitlines()
            diagnostic = f"：{detail[-1][-180:]}" if detail else ""
            raise PersonalAgentError(
                "artifact_process_failed",
                f"隔离计算执行失败，退出码 {result['exit_code']}{diagnostic}",
            )
        published = []
        for item in result["files"]:
            if item_context is not None:
                await item_context.authorize()
            filename = Path(item["path"]).name
            snapshot = await service.publish(
                actor_id=actor,
                request_id="process-" + self.call_id + "-" + str(len(published)),
                title=filename,
                kind="output",
                filename=filename,
                content=item["content"],
                source_refs=refs,
                session_id=session_id,
                _trusted_execution=True,
                evidence={
                    "source_kind": "isolated_process",
                    "session_id": session_id,
                    "call_id": self.call_id,
                    "run_id": run_id,
                    "isolation": result["isolation"],
                },
            )
            url = "/web2?" + urlencode(
                {
                    "artifact": snapshot["artifact_id"],
                    "version": snapshot["version"],
                    "actor": actor,
                }
            )
            published.append({**snapshot, "url": url, "markdown": f"[{filename}]({url})"})
            outputs = getattr(self.engine, "_personal_artifact_outputs", [])
            outputs.append(service.reference(snapshot))
            self.engine._personal_artifact_outputs = outputs
        return json.dumps(
            {
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "exit_code": result["exit_code"],
                "artifacts": published,
                "isolation": result["isolation"],
            },
            ensure_ascii=False,
        )

    async def _read_input(self, actor, refs, args):
        """Resolve exact Linux input paths through the broker, never through host tools."""
        requested = args.get("path", args.get("file_path"))
        service = self.engine.personal_artifacts
        for ref in refs:
            snapshot, content = await service.read(
                actor_id=actor,
                artifact_id=ref["artifact_id"],
                version=ref["version"],
            )
            expected = f"/inputs/{ref['artifact_id']}-v{ref['version']}{snapshot['suffix']}"
            if requested == expected:
                from naumi_agent.personal.artifact_models import is_text_artifact

                if not is_text_artifact(snapshot):
                    raise PersonalAgentError("invalid_input", "该资料需要通过隔离计算读取")
                value = content.decode("utf-8-sig")
                offset = args.get("offset", 0)
                limit = args.get("limit", 20000)
                if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
                    raise PersonalAgentError("invalid_input", "资料读取位置或长度无效")
                limit = min(limit, 60000)
                return json.dumps(
                    {
                        "path": expected,
                        "ref": ref,
                        "content": value[offset : offset + limit],
                        "offset": offset,
                        "next_offset": offset + limit if len(value) > offset + limit else None,
                    },
                    ensure_ascii=False,
                )
        raise PersonalAgentError("artifact_access_revoked", "该路径不是本次明确授权的输入资料")

    async def _record_process(self, value, status, *, run_id):
        if not run_id:
            return ""
        identity = hashlib.sha256((run_id + ":" + self.call_id).encode()).hexdigest()[:32]
        await self.engine.chat_run_store.append_artifact(
            run_id,
            kind="artifact_process",
            title="隔离计算",
            summary=value,
            status=status,
            artifact_id="process-" + identity,
            metadata={"call_id": self.call_id, "tool_name": self.name},
        )
        return run_id


def guarded_tool(engine, tool, call_id):
    service = getattr(engine, "personal_artifacts", None)
    memory = getattr(engine, "personal_memory", None)
    if service is None or not (
        service.enabled or (memory is not None and memory.enabled)
        or getattr(engine, "_personal_item_context", None) is not None
    ):
        return tool
    return ArtifactToolProxy(engine, tool, call_id)
