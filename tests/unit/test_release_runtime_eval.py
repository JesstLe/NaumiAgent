from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.runtime_eval import (
    MAX_RUNTIME_EVAL_INPUT_BYTES,
    ReleaseRuntimeEvalCaseRequest,
    ReleaseRuntimeEvalError,
    ReleaseRuntimeEvalResponse,
    build_protocol_hello_request,
    build_runtime_eval_process_request,
    build_runtime_eval_receipt,
    execute_runtime_eval_request,
    parse_and_execute_runtime_eval,
)
from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore, host_release_target


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _runtime_bundle(root: Path) -> Path:
    target = host_release_target()
    if target.startswith("windows-"):
        pytest.skip("该真实进程 fixture 需要 POSIX executable。")
    python = Path(sys.executable).absolute()
    source_root = Path(__file__).resolve().parents[2]
    backend = root / "backend"
    runtime = _binary(
        backend / "naumi-runtime",
        (
            f"#!{python}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(source_root)!r})\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('naumi 1.0.0')\n"
            "elif sys.argv[1:] == ['--runtime-eval-json']:\n"
            "    from naumi_agent.release.runtime_eval import parse_and_execute_runtime_eval\n"
            "    sys.stdout.buffer.write(parse_and_execute_runtime_eval(sys.stdin.buffer.read()))\n"
            "else:\n"
            "    raise SystemExit(64)\n"
        ).encode(),
    )
    launcher = _binary(root / "launcher" / "naumi", b"#!/bin/sh\nexit 0\n")
    ui = _binary(root / "ui" / "naumi-ui", b"#!/bin/sh\nexit 0\n")
    config = root / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    return assemble_release_artifact(
        backend_dir=runtime.parent,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=root / "release",
        version="1.0.0",
        target=target,
        source_commit="a" * 40,
        source_tree_sha256="b" * 64,
        archive_format="tar.gz",
    ).bundle_dir


def _request(workspace: Path):
    return build_protocol_hello_request(
        workspace,
        workspace / "docs/harness/evals/protocol-hello-core.yaml",
    )


def test_runtime_eval_request_executes_all_real_protocol_fixtures() -> None:
    workspace = Path(__file__).resolve().parents[2]
    request = _request(workspace)
    process_request = build_runtime_eval_process_request(request)

    response = execute_runtime_eval_request(
        request,
        evaluated_at="2026-08-10T08:00:00+00:00",
    )
    decoded = ReleaseRuntimeEvalResponse.model_validate_json(
        parse_and_execute_runtime_eval(process_request.model_dump_json().encode("utf-8"))
    )

    assert request.suite_id == "protocol-hello-core"
    assert len(request.cases) == 6
    assert len(response.results) == 6
    assert decoded.authority_request_sha256 == request.request_sha256
    assert '"expected"' not in process_request.model_dump_json()
    with pytest.raises(ReleaseRuntimeEvalError) as oversized:
        parse_and_execute_runtime_eval(b"{" + b"x" * MAX_RUNTIME_EVAL_INPUT_BYTES)
    assert oversized.value.code == "release_runtime_eval_input_oversized"
    with pytest.raises(ValueError):
        ReleaseRuntimeEvalCaseRequest(
            case_id="secret-case",
            fixture_sha256="1" * 64,
            payload_sha256="2" * 64,
            payload={"api_token": "must-not-persist"},
            expected=request.cases[0].expected,
            max_duration_ms=100,
        )

    forged = response.model_dump(mode="json")
    forged["results"][0]["actual"] = {
        "outcome": "rejected",
        "error_code": "bad_request",
        "selected_version": None,
        "capabilities": [],
    }
    core = {
        key: value
        for key, value in forged.items()
        if key not in {"response_id", "response_sha256"}
    }
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    forged_response = ReleaseRuntimeEvalResponse.model_validate({
        **core,
        "response_id": f"relruntimeevalresp_{digest[:24]}",
        "response_sha256": digest,
    })
    failed_receipt = build_runtime_eval_receipt(
        slot_id="relslot_" + "1" * 24,
        slot_sha256="2" * 64,
        manifest_sha256="3" * 64,
        binary_sha256="4" * 64,
        request=request,
        process_request=process_request,
        response=forged_response,
        input_bytes=1,
        output_bytes=1,
    )
    assert not failed_receipt.all_cases_passed
    assert failed_receipt.passed_cases == 5


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_installed_slot_executes_and_persists_binary_bound_runtime_eval(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[2]
    request = _request(workspace)
    store = ReleaseSlotStore(tmp_path / "installed")
    slot = store.install(_runtime_bundle(tmp_path))
    store.verify_bootable(slot.slot_id)

    receipt = store.evaluate_runtime_protocol(slot.slot_id, request)
    restored = store.get_runtime_eval_receipt(receipt.receipt_id)

    assert receipt.runtime_process_executed
    assert receipt.all_cases_passed
    assert receipt.request == request
    assert len(receipt.response.results) == 6
    assert restored == receipt
    assert os.environ.get("NAUMI_RELEASE_RUNTIME_EVAL") is None

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE release_runtime_eval_receipts SET receipt_json = ? "
            "WHERE receipt_id = ?",
            (json.dumps({"schema_version": 1}), receipt.receipt_id),
        )
        db.commit()
    with pytest.raises(ReleaseSlotError) as corrupt:
        store.get_runtime_eval_receipt(receipt.receipt_id)
    assert corrupt.value.code == "release_runtime_eval_receipt_corrupt"


def test_runtime_cli_requires_controlled_environment_and_emits_only_json() -> None:
    workspace = Path(__file__).resolve().parents[2]
    request = _request(workspace)
    process_request = build_runtime_eval_process_request(request)
    command = [sys.executable, "-m", "naumi_agent.main", "--runtime-eval-json"]
    denied = subprocess.run(
        command,
        input=process_request.model_dump_json().encode("utf-8"),
        cwd=workspace,
        capture_output=True,
        check=False,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "NAUMI_RELEASE_RUNTIME_EVAL"
        },
    )
    allowed = subprocess.run(
        command,
        input=process_request.model_dump_json().encode("utf-8"),
        cwd=workspace,
        capture_output=True,
        check=False,
        env={**os.environ, "NAUMI_RELEASE_RUNTIME_EVAL": "1", "NO_COLOR": "1"},
    )

    assert denied.returncode == 78
    assert b"release_runtime_eval_environment_missing" in denied.stderr
    assert allowed.returncode == 0 and not allowed.stderr
    response = ReleaseRuntimeEvalResponse.model_validate_json(allowed.stdout)
    assert response.authority_request_id == request.request_id
    assert len(response.results) == 6


def test_runtime_eval_rejects_non_protocol_suite(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[2]
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "schema_version: 1\nid: invalid\ntitle: invalid\ncases: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ReleaseRuntimeEvalError) as invalid:
        build_protocol_hello_request(workspace, suite)
    assert invalid.value.code == "release_runtime_eval_suite_outside_workspace"


def test_runtime_eval_rejects_unknown_slot_before_process(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("fixture is a POSIX executable")
    workspace = Path(__file__).resolve().parents[2]
    store = ReleaseSlotStore(tmp_path / "installed")
    bundle = _runtime_bundle(tmp_path)
    slot = store.install(bundle)
    request = _request(workspace)
    with pytest.raises(ReleaseSlotError) as missing:
        store.evaluate_runtime_protocol("relslot_" + "0" * 24, request)
    assert missing.value.code == "release_slot_missing"
    assert store.get_slot(slot.slot_id) == slot
