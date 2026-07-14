from __future__ import annotations

import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from naumi_agent.release.artifact import ArtifactError, assemble_release_artifact

ROOT = Path(__file__).resolve().parents[2]


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def test_assemble_release_artifact_creates_manifest_archive_and_checksum(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    _binary(backend / "naumi", b"frozen-backend")
    _binary(backend / "_internal" / "libagent.dylib", b"runtime-library")
    ui = _binary(tmp_path / "naumi-ui", b"compiled-terminal-ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")

    result = assemble_release_artifact(
        backend_dir=backend,
        ui_binary=ui,
        config_example=config,
        output_dir=tmp_path / "release",
        version="1.2.3",
        target="macos-arm64",
        archive_format="tar.gz",
    )

    assert result.bundle_dir.name == "naumi-1.2.3-macos-arm64"
    assert result.archive.name == "naumi-1.2.3-macos-arm64.tar.gz"
    manifest = json.loads((result.bundle_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["version"] == "1.2.3"
    assert manifest["target"] == "macos-arm64"
    files = {item["path"]: item for item in manifest["files"]}
    assert files["naumi"]["sha256"] == hashlib.sha256(b"frozen-backend").hexdigest()
    assert files["naumi-ui"]["sha256"] == hashlib.sha256(b"compiled-terminal-ui").hexdigest()
    assert all(not path.endswith((".py", ".pyc", ".js", ".ts")) for path in files)
    expected_checksum = hashlib.sha256(result.archive.read_bytes()).hexdigest()
    assert result.checksum.read_text().strip() == f"{expected_checksum}  {result.archive.name}"
    with tarfile.open(result.archive, "r:gz") as archive:
        names = archive.getnames()
    assert f"{result.bundle_dir.name}/manifest.json" in names


def test_assemble_release_artifact_rejects_project_source_without_replacing_release(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    _binary(backend / "naumi", b"backend")
    (backend / "secret.py").write_text("print('source leak')\n", encoding="utf-8")
    ui = _binary(tmp_path / "naumi-ui", b"ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    existing = tmp_path / "release" / "naumi-1.2.3-linux-x64"
    existing.mkdir(parents=True)
    sentinel = existing / "installed.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ArtifactError, match="源码泄漏"):
        assemble_release_artifact(
            backend_dir=backend,
            ui_binary=ui,
            config_example=config,
            output_dir=tmp_path / "release",
            version="1.2.3",
            target="linux-x64",
            archive_format="tar.gz",
        )

    assert sentinel.read_text() == "keep"


def test_assemble_windows_zip_renames_ui_and_is_reproducible(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    _binary(backend / "naumi.exe", b"windows-backend")
    ui = _binary(tmp_path / "terminal-ui-build.exe", b"windows-ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")

    first = assemble_release_artifact(
        backend_dir=backend,
        ui_binary=ui,
        config_example=config,
        output_dir=tmp_path / "release-a",
        version="1.2.3",
        target="windows-x64",
        archive_format="zip",
    )
    second = assemble_release_artifact(
        backend_dir=backend,
        ui_binary=ui,
        config_example=config,
        output_dir=tmp_path / "release-b",
        version="1.2.3",
        target="windows-x64",
        archive_format="zip",
    )

    assert first.archive.read_bytes() == second.archive.read_bytes()
    with zipfile.ZipFile(first.archive) as archive:
        names = archive.namelist()
    assert f"{first.bundle_dir.name}/naumi.exe" in names
    assert f"{first.bundle_dir.name}/naumi-ui.exe" in names


def test_assembler_allows_declared_third_party_runtime_data_but_not_naumi_source(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    _binary(backend / "naumi", b"backend")
    third_party = backend / "_internal" / "vendor" / "runtime.py"
    third_party.parent.mkdir(parents=True)
    third_party.write_text("VENDOR_DATA = True\n", encoding="utf-8")
    naumi_source = backend / "_internal" / "naumi_agent" / "secret.py"
    naumi_source.parent.mkdir(parents=True)
    naumi_source.write_text("SECRET = True\n", encoding="utf-8")
    ui = _binary(tmp_path / "naumi-ui", b"ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="naumi_agent/secret.py"):
        assemble_release_artifact(
            backend_dir=backend,
            ui_binary=ui,
            config_example=config,
            output_dir=tmp_path / "release",
            version="1.2.3",
            target="linux-x64",
            archive_format="tar.gz",
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privileges vary")
def test_assemble_release_artifact_rejects_symlink_escape(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    _binary(backend / "naumi", b"backend")
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    (backend / "escape").symlink_to(outside)
    ui = _binary(tmp_path / "naumi-ui", b"ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="符号链接越界"):
        assemble_release_artifact(
            backend_dir=backend,
            ui_binary=ui,
            config_example=config,
            output_dir=tmp_path / "release",
            version="1.2.3",
            target="linux-x64",
            archive_format="tar.gz",
        )


def test_release_builder_collects_runtime_plugins_and_runs_real_bridge_smoke() -> None:
    spec = (ROOT / "packaging" / "naumi.spec").read_text(encoding="utf-8")
    unix = (ROOT / "scripts" / "release" / "build_unix.sh").read_text(
        encoding="utf-8"
    )
    windows = (ROOT / "scripts" / "release" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert '"tiktoken_ext"' in spec
    assert "collect_all" not in spec
    assert "verify_frozen_bridge.py" in unix
    assert "verify_frozen_bridge.py" in windows


def test_release_workflow_covers_mainstream_targets_without_uploading_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-binaries.yml").read_text(
        encoding="utf-8"
    )

    for target in (
        "linux-x64",
        "linux-arm64",
        "macos-x64",
        "macos-arm64",
        "windows-x64",
    ):
        assert f"target: {target}" in workflow
    assert "DISTRIBUTION_GITHUB_TOKEN" in workflow
    assert "--prerelease" in workflow
    assert "release-output/*.tar.gz" in workflow
    assert "release-output/*.zip" in workflow
    assert "path: ." not in workflow
