"""Fail-closed container execution for private artifact calculations.

Private work uses a bounded public-workspace snapshot, exact input bytes and a
separate output mount. Nothing receives a host workspace, daemon socket or model
credentials. Docker failure is never translated into native host execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from naumi_agent.personal.models import PersonalAgentError

_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")
_LABEL = "io.naumi.artifact.run"
_SNAPSHOT_BYTES = 256 * 1024 * 1024
_SNAPSHOT_FILES = 20000
_TREE_ENTRIES = 100000
_OUTPUT_BYTES = 20 * 1024 * 1024
_OUTPUT_FILES = 32
_OUTPUT_ENTRIES = 512
_EXCLUDED = frozenset(
    {
        ".naumi",
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_DOCKER_ENV = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
)


def _error(code: str, message: str) -> PersonalAgentError:
    return PersonalAgentError(code, message)


def _relative(value: Any) -> str:
    if not isinstance(value, str) or value in {"", ".", ".."} or len(value) > 240:
        raise _error("invalid_input", "资料文件名需要有效的相对路径")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in {"", ".", ".."}
            or len(part) > 128
            or part.endswith((" ", "."))
            or part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)],
            }
            for part in parsed.parts
        )
        or len(parsed.parts) > 16
    ):
        raise _error("invalid_input", "资料文件名包含越界或不支持的路径")
    return value


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _prepare_workspace(workspace: Path, destination: Path, private: bool, protected: list[Path]):
    excluded: set[str] = set()
    counters = {"files": 0, "bytes": 0, "entries": 0}
    masks: list[tuple[str, bool]] = []
    for root in protected:
        if workspace == root or workspace.is_relative_to(root):
            raise _error("isolation_denied", "私有资料目录不能作为隔离工作区")
        if root.is_relative_to(workspace):
            relative = root.relative_to(workspace).as_posix()
            if not private and not root.exists():
                raise _error("isolation_denied", "可写隔离工作区缺少受保护目录的挂载点")
            masks.append((relative, root.is_file()))
    if not private:
        for name in (".naumi", ".git"):
            target = workspace / name
            if not target.exists():
                raise _error(
                    "isolation_denied", "可写隔离工作区需要既有的 .naumi 与 .git 保护挂载点"
                )
            masks.append((name, target.is_file()))
    else:
        destination.mkdir(mode=0o755)

    def visit(source: Path, target: Path, ancestors: frozenset[Path], depth: int):
        if depth > 64:
            raise _error("workspace_limit", "工作区目录层级过深，请明确提供本次资料")
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(workspace) or _inside(resolved, protected):
            raise _error("isolation_denied", "工作区链接指向未授权位置，请明确提供本次资料")
        if resolved in ancestors:
            raise _error("isolation_denied", "工作区包含循环目录链接")
        ancestors = ancestors | {resolved}
        with os.scandir(resolved) as entries:
            for entry in entries:
                counters["entries"] += 1
                if counters["entries"] > _TREE_ENTRIES:
                    raise _error("workspace_limit", "工作区文件过多，请明确提供本次资料")
                item = Path(entry.path)
                logical = target / entry.name
                relative = logical.relative_to(destination).as_posix()
                if private and (entry.name in _EXCLUDED or _inside(item, protected)):
                    excluded.add(relative)
                    continue
                if not private and any(
                    relative == mask or relative.startswith(mask + "/") for mask, _ in masks
                ):
                    excluded.add(relative)
                    continue
                # DirEntry.stat() can report zero inode/link counters on Windows.
                # Full lstat is required for actual link and identity checks.
                info = item.lstat()
                actual = item.resolve(strict=True)
                if not actual.is_relative_to(workspace) or _inside(actual, protected):
                    raise _error("isolation_denied", "工作区链接指向未授权位置，请明确提供本次资料")
                actual_parts = actual.relative_to(workspace).parts
                if private and any(part in _EXCLUDED for part in actual_parts):
                    raise _error("isolation_denied", "工作区链接指向受保护或已排除的目录")
                if stat.S_ISLNK(info.st_mode) or _reparse(info):
                    info = actual.stat()
                if stat.S_ISDIR(info.st_mode):
                    if private:
                        logical.mkdir(mode=0o755)
                    visit(actual, logical, ancestors, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise _error(
                            "isolation_denied", "工作区包含硬链接，请通过资料版本明确提供文件"
                        )
                    counters["files"] += 1
                    counters["bytes"] += info.st_size
                    if counters["files"] > _SNAPSHOT_FILES or counters["bytes"] > _SNAPSHOT_BYTES:
                        raise _error(
                            "workspace_limit", "工作区超过隔离快照限额，请明确提供本次资料"
                        )
                    if private:
                        with actual.open("rb") as reader, logical.open("xb") as writer:
                            opened = os.fstat(reader.fileno())
                            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_nlink) != (
                                info.st_dev,
                                info.st_ino,
                                info.st_size,
                                1,
                            ):
                                raise _error("isolation_denied", "工作区文件在读取时发生变化")
                            copied = 0
                            while chunk := reader.read(65536):
                                copied += len(chunk)
                                if copied > info.st_size:
                                    raise _error("isolation_denied", "工作区文件在读取时发生变化")
                                writer.write(chunk)
                            if copied != info.st_size:
                                raise _error("isolation_denied", "工作区文件在读取时发生变化")
                        logical.chmod(0o644)
                else:
                    raise _error("isolation_denied", "工作区包含不支持的特殊文件")

    visit(workspace, destination, frozenset(), 0)
    return {**counters, "excluded_dirs": sorted(excluded)}, sorted(set(masks))


def _collect_outputs(root: Path) -> list[dict]:
    files, total, entries_count = [], 0, 0

    def visit(directory: Path, depth: int):
        nonlocal total, entries_count
        if depth > 16:
            raise _error("invalid_output", "执行结果目录层级过深")
        with os.scandir(directory) as entries:
            for entry in entries:
                entries_count += 1
                if entries_count > _OUTPUT_ENTRIES:
                    raise _error("output_limit", "执行结果目录或文件过多")
                path = Path(entry.path)
                info = path.lstat()
                relative = _relative(path.relative_to(root).as_posix())
                if (
                    stat.S_ISLNK(info.st_mode)
                    or _reparse(info)
                    or not path.resolve().is_relative_to(root)
                ):
                    raise _error("invalid_output", "执行结果包含越界链接")
                if stat.S_ISDIR(info.st_mode):
                    visit(path, depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                    if len(files) >= _OUTPUT_FILES or total > _OUTPUT_BYTES:
                        raise _error("output_limit", "执行结果超过 32 个文件或 20 MB 限额")
                    with path.open("rb") as stream:
                        actual = os.fstat(stream.fileno())
                        if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_nlink) != (
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            1,
                        ):
                            raise _error("invalid_output", "执行结果在读取时发生变化")
                        content = stream.read(info.st_size + 1)
                        if len(content) != info.st_size:
                            raise _error("invalid_output", "执行结果在读取时发生变化")
                    files.append({"path": relative, "content": content})
                else:
                    raise _error("invalid_output", "执行结果包含硬链接或特殊文件")

    visit(root, 0)
    return sorted(files, key=lambda item: item["path"])


async def _disk(function, *args):
    # A cancelled to_thread future does not stop its file operations. Finish the
    # bounded operation before removing its exclusively owned staging directory.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def _remove_stage(stage: Path, parent: Path):
    if (
        stage.parent != parent
        or not stage.name.startswith("naumi-artifact-")
        or stage.resolve() != stage
    ):
        raise _error("cleanup_unknown", "隔离临时目录身份待核对，未删除其他目录")

    def readonly(function, filename, error):
        if not isinstance(error, PermissionError):
            raise error
        path = Path(filename)
        if not path.absolute().is_relative_to(stage):
            raise _error("cleanup_unknown", "隔离临时文件身份待核对")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or _reparse(info):
            if path.is_dir():
                os.rmdir(path)
            else:
                path.unlink()
            return
        if not path.resolve().is_relative_to(stage):
            raise _error("cleanup_unknown", "隔离临时文件身份待核对")
        path.chmod(0o700 if path.is_dir() else 0o600)
        function(filename)

    shutil.rmtree(stage, onexc=readonly)


async def _command(docker: str, args: list[str], *, timeout: float = 10, limit: int = 65536):
    process = await asyncio.create_subprocess_exec(
        docker,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env={key: value for key, value in os.environ.items() if key.upper() in _DOCKER_ENV},
    )

    async def read(stream):
        captured = bytearray()
        truncated = False
        while chunk := await stream.read(65536):
            room = limit - len(captured)
            captured.extend(chunk[: max(0, room)])
            truncated = truncated or len(chunk) > room
        return bytes(captured).decode("utf-8", "replace"), truncated

    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
        return process.returncode, stdout[0], stderr[0], stdout[1], stderr[1]
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise


async def _require_local_engine(docker: str) -> str:
    """Bind mounts must refer to this broker's files, never a remote host path."""
    context = os.environ.get("DOCKER_CONTEXT", "").strip()
    endpoint = os.environ.get("DOCKER_HOST", "").strip() if not context else ""
    if not endpoint:
        if context and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
            raise _error("isolation_unavailable", "私有隔离需要有效的本机 Docker 上下文")
        try:
            record = await _command(
                docker,
                [
                    "context",
                    "inspect",
                    *([context] if context else []),
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ],
            )
            if record[0] != 0 or record[3] or record[4]:
                raise ValueError("Docker endpoint is not authoritative")
            endpoint = json.loads(record[1])
        except (OSError, TimeoutError, ValueError, TypeError) as error:
            raise _error(
                "isolation_unavailable", "本机 Docker 连接待核对，本次未执行代码"
            ) from error
    if isinstance(endpoint, str) and endpoint.startswith("unix:///"):
        return "local_socket"
    if isinstance(endpoint, str) and endpoint.lower().startswith("npipe:////./pipe/"):
        return "local_pipe"
    raise _error("isolation_unavailable", "私有隔离只支持本机 Docker 连接，不向远端挂载资料路径")


async def _cleanup(docker: str, name: str, nonce: str) -> None:
    try:
        await _cleanup_owned(docker, name, nonce)
    except PersonalAgentError:
        raise
    except (OSError, TimeoutError, ValueError, TypeError) as error:
        raise _error("cleanup_unknown", "隔离进程清理状态待核对，请检查原记录") from error


async def _cleanup_owned(docker: str, name: str, nonce: str) -> None:
    filters = ["--filter", f"label={_LABEL}={nonce}", "--filter", f"name=^/{name}$"]
    result = await _command(
        docker, ["container", "ls", "--all", "--no-trunc", *filters, "--format", "{{.ID}}"]
    )
    if result[0] != 0 or result[3] or result[4]:
        raise _error("cleanup_unknown", "隔离进程清理状态待核对，请恢复 Docker 后检查原记录")
    identities = result[1].split()
    if len(identities) > 1 or any(not _CONTAINER_ID.fullmatch(identity) for identity in identities):
        raise _error("cleanup_unknown", "隔离进程身份待核对，未操作其他容器")
    for identity in identities:
        inspected = await _command(
            docker, ["inspect", identity, "--format", "{{json .Config.Labels}}"]
        )
        labels = json.loads(inspected[1]) if inspected[0] == 0 else None
        if (
            not isinstance(labels, dict)
            or labels.get(_LABEL) != nonce
            or inspected[3]
            or inspected[4]
        ):
            raise _error("cleanup_unknown", "隔离进程身份待核对，未操作其他容器")
        removed = await _command(docker, ["rm", "--force", identity])
        if removed[0] != 0:
            raise _error("cleanup_unknown", "隔离进程尚未确认清理，请恢复 Docker 后核对原记录")
    remaining = await _command(
        docker, ["container", "ls", "--all", "--no-trunc", *filters, "--format", "{{.ID}}"]
    )
    if remaining[0] != 0 or remaining[1].strip() or remaining[3] or remaining[4]:
        raise _error("cleanup_unknown", "隔离进程尚未确认清理，请核对原记录")


async def execute_private_process(
    *,
    workspace: Path,
    command: str,
    language: str = "shell",
    inputs: dict[str, bytes] | None = None,
    private: bool = True,
    empty_workspace: bool = False,
    timeout_s: float = 60,
    image: str = "python:3.12-slim",
    max_output_bytes: int = 1048576,
    protected_roots: list[Path] | None = None,
) -> dict:
    """Compute in Docker; private mode reads a snapshot and emits only /outputs files."""
    if (
        type(private) is not bool
        or type(empty_workspace) is not bool
        or empty_workspace and not private
        or language not in {"shell", "python", "javascript"}
        or not isinstance(command, str)
        or not command.strip()
        or len(command) > 100000
        or "\0" in command
        or isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not 0 < timeout_s <= 300
        or type(max_output_bytes) is not int
        or not 1 <= max_output_bytes <= 8 * 1024 * 1024
        or not isinstance(image, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", image)
    ):
        raise _error("invalid_input", "隔离执行参数不正确，请检查代码、语言、镜像与限额")
    raw_workspace = Path(workspace).expanduser()
    if raw_workspace.is_symlink() or (raw_workspace.exists() and _reparse(raw_workspace.lstat())):
        raise _error("isolation_denied", "隔离工作区不能使用目录链接")
    try:
        workspace = raw_workspace.resolve(strict=True)
    except OSError as error:
        raise _error("invalid_input", "隔离工作区不存在或暂不可读") from error
    if not workspace.is_dir() or "," in str(workspace):
        raise _error("invalid_input", "隔离工作区需要有效的目录路径")
    if protected_roots is not None and not isinstance(protected_roots, list):
        raise _error("invalid_input", "受保护目录需要路径列表")
    try:
        protected = [Path(root).expanduser().resolve() for root in protected_roots or []]
    except (TypeError, ValueError, OSError) as error:
        raise _error("invalid_input", "受保护目录需要有效路径") from error
    values = {} if inputs is None else inputs
    file_limit = 64 if empty_workspace else 32
    if not isinstance(values, dict) or len(values) > file_limit:
        raise _error("invalid_input", f"隔离资料需要不超过 {file_limit} 个文件")
    total = 0
    names = set()
    for name, content in values.items():
        _relative(name)
        if not isinstance(content, bytes):
            raise _error("invalid_input", "隔离资料需要确切文件字节")
        if empty_workspace and len(content) > 20 * 1024**2:
            raise _error("invalid_input", "单份事项资料超过 20 MB")
        normalized = name.casefold()
        if normalized in names or any(
            normalized.startswith(n + "/") or n.startswith(normalized + "/") for n in names
        ):
            raise _error("invalid_input", "隔离资料文件名重复或相互覆盖")
        names.add(normalized)
        total += len(content)
    total_limit = 50 * 1024**2 if empty_workspace else _OUTPUT_BYTES
    if total > total_limit:
        raise _error("invalid_input", "本次隔离资料超过授权大小上限，请分开处理")
    docker = shutil.which("docker")
    if docker is None:
        raise _error(
            "isolation_unavailable", "隔离执行需要正在运行的 Docker，当前未执行任何宿主命令"
        )
    endpoint_kind = await _require_local_engine(docker)
    nonce = uuid.uuid4().hex
    name = "naumi-artifact-" + nonce
    isolation = {
        "backend": "docker",
        "run_id": nonce,
        "image_id": "",
        "container_id": "",
        "container_removed": False,
    }
    started = time.monotonic()
    temp_base = Path(tempfile.gettempdir()).resolve()
    if "," in str(temp_base):
        raise _error("invalid_input", "隔离临时目录不能包含逗号")
    stage = Path(tempfile.mkdtemp(prefix="naumi-artifact-", dir=temp_base)).resolve()
    attempted, cancelled, failure, result = False, False, None, None
    try:
        stage.chmod(0o700)
        snapshot, in_dir, out_dir = stage / "workspace", stage / "inputs", stage / "outputs"
        in_dir.mkdir(mode=0o755)
        out_dir.mkdir(mode=0o777)
        out_dir.chmod(0o777)
        if empty_workspace:
            snapshot.mkdir(mode=0o755)
            workspace_info = {"files": 0, "bytes": 0, "entries": 0, "excluded_dirs": []}
            masks = []
        else:
            workspace_info, masks = await _disk(
                _prepare_workspace, workspace, snapshot, private, protected
            )
        if any("," in relative for relative, _ in masks):
            raise _error("invalid_input", "受保护目录名称不能包含逗号")
        for filename, content in values.items():
            destination = in_dir / filename
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            destination.write_bytes(content)
            destination.chmod(0o644)
        extension = {"shell": "sh", "python": "py", "javascript": "js"}[language]
        payload = stage / ("payload." + extension)
        payload.write_text(command, encoding="utf-8", newline="\n")
        payload.chmod(0o644)
        inspected = await _command(docker, ["image", "inspect", image, "--format", "{{.Id}}"])
        image_id = inspected[1].strip()
        if inspected[0] != 0 or not _IMAGE_ID.fullmatch(image_id):
            raise _error(
                "isolation_unavailable", "隔离镜像尚不可用，请先准备本机镜像；本次没有执行宿主命令"
            )
        isolation["image_id"] = image_id
        run_args = [
            "create",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{_LABEL}={nonce}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--ipc",
            "none",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--mount",
            f"type=bind,source={snapshot if private else workspace},target=/workspace"
            + (",readonly" if private else ""),
            "--mount",
            f"type=bind,source={in_dir},target=/inputs,readonly",
            "--mount",
            f"type=bind,source={out_dir},target=/outputs",
            "--mount",
            f"type=bind,source={payload},target=/naumi-payload.{extension},readonly",
        ]
        for index, (relative, is_file) in enumerate(masks if not private else []):
            empty = stage / ("mask-" + str(index))
            if is_file:
                empty.write_bytes(b"")
            else:
                empty.mkdir(mode=0o755)
            run_args.extend(
                ["--mount", f"type=bind,source={empty},target=/workspace/{relative},readonly"]
            )
        executable = {"shell": ["/bin/sh"], "python": ["python", "-I"], "javascript": ["node"]}[
            language
        ]
        attempted = True
        created = await _command(
            docker, [*run_args, image_id, *executable, f"/naumi-payload.{extension}"]
        )
        container_id = created[1].strip()
        if created[0] != 0 or not _CONTAINER_ID.fullmatch(container_id):
            raise _error("isolation_unavailable", "隔离进程未成功启动，本次没有执行宿主命令")
        isolation["container_id"] = container_id
        attached = await _command(
            docker, ["start", "--attach", container_id], timeout=timeout_s, limit=max_output_bytes
        )
        state_result = await _command(
            docker, ["inspect", container_id, "--format", "{{json .State}}"]
        )
        if state_result[0] != 0 or state_result[3] or state_result[4]:
            raise _error("execution_unknown", "隔离进程终态待核对，请保留原执行记录")
        try:
            state = json.loads(state_result[1])
        except ValueError as error:
            raise _error("execution_unknown", "隔离进程终态待核对，请保留原执行记录") from error
        if (
            not isinstance(state, dict)
            or state.get("Running")
            or state.get("Error")
            or state.get("Status") != "exited"
            or type(state.get("ExitCode")) is not int
            or not 0 <= state["ExitCode"] <= 255
        ):
            raise _error("execution_unknown", "隔离进程没有可信退出状态，请核对原执行记录")
        result = {
            "stdout": attached[1],
            "stderr": attached[2],
            "exit_code": state["ExitCode"],
            "stdout_truncated": attached[3],
            "stderr_truncated": attached[4],
            "isolation": {
                **isolation,
                "backend": "docker",
                "image_id": image_id,
                "container_id": container_id,
                "network": "none",
                "endpoint_kind": endpoint_kind,
                "user": "65534:65534",
                "read_only_rootfs": True,
                "workspace_snapshot": private,
                "workspace_read_only": private,
                "workspace_files": workspace_info["files"],
                "workspace_bytes": workspace_info["bytes"],
                "excluded_dirs": workspace_info["excluded_dirs"],
                "input_sha256": {
                    key: hashlib.sha256(value).hexdigest() for key, value in values.items()
                },
            },
        }
    except TimeoutError:
        failure = _error("execution_timeout", "隔离执行已超时，进程已进入清理，不会转到宿主执行")
    except OSError as error:
        failure = _error(
            "isolation_unavailable", "隔离文件或 Docker 暂不可用，本次没有转到宿主执行"
        )
        failure.__cause__ = error
    except BaseException as error:
        failure = error
    try:
        if attempted:
            task = asyncio.create_task(_cleanup(docker, name, nonce))
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
        isolation["container_removed"] = True
        if failure is not None:
            if isinstance(failure, PersonalAgentError):
                failure.isolation = dict(isolation)
            raise failure
        if cancelled:
            raise asyncio.CancelledError
        result["isolation"]["container_removed"] = True
        result["files"] = await _disk(_collect_outputs, stage / "outputs")
        result["isolation"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result
    except PersonalAgentError as error:
        error.isolation = dict(isolation)
        raise
    finally:
        # Only this mkdtemp root is removed, never an input/project/user path.
        _remove_stage(stage, temp_base)
