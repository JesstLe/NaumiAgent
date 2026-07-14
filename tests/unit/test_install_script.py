from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from naumi_agent.release.artifact import assemble_release_artifact

ROOT = Path(__file__).resolve().parents[2]


def _unix_script() -> str:
    return (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")


def _windows_script() -> str:
    return (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


def test_binary_installer_never_clones_or_installs_project_source() -> None:
    script = _unix_script()

    assert "git clone" not in script
    assert "git pull" not in script
    assert "pip install" not in script
    assert "uv sync" not in script
    assert "NaumiAgent-Releases" in script


def test_binary_installer_maps_supported_unix_platforms_and_architectures() -> None:
    script = _unix_script()

    assert "Darwin) platform=macos" in script
    assert "Linux) platform=linux" in script
    assert "x86_64|amd64) arch=x64" in script
    assert "arm64|aarch64) arch=arm64" in script
    assert "install.ps1" in script


def test_binary_installer_verifies_checksum_before_extracting() -> None:
    script = _unix_script()

    download = script.index("curl --fail")
    checksum = script.index('actual=$(shasum')
    extract = script.index('tar -xzf "$tmp/$asset"')
    assert download < checksum < extract
    assert "--proto '=https' --tlsv1.2" in script
    assert 'SHA-256 校验失败，已拒绝安装' in script
    assert 'tar -tzf "$tmp/$asset"' in script
    assert "安装包含不安全路径" in script


def test_binary_installer_keeps_versioned_releases_and_atomic_links() -> None:
    script = _unix_script()

    assert 'mkdir -p "$INSTALL_ROOT/releases"' in script
    assert 'destination="$INSTALL_ROOT/releases/$(basename "$bundle")"' in script
    assert 'ln -s "$destination" "$INSTALL_ROOT/current.new"' in script
    assert 'mv -f "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"' in script
    assert 'rm -f "$INSTALL_ROOT/current.new" "$BIN_DIR/naumi.new"' in script
    assert '该版本已安装' in script


def test_windows_installer_verifies_sha256_and_uses_binary_zip() -> None:
    script = _windows_script()

    assert "naumi-windows-$Arch.zip" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "Expand-Archive" in script
    assert script.index("Get-FileHash") < script.index("Expand-Archive")
    assert "[System.IO.Compression.ZipFile]::OpenRead" in script
    assert "安装包含不安全路径" in script
    assert "git clone" not in script
    assert "pip install" not in script


def test_unix_installer_installs_verified_fixture_and_preserves_it_on_repeat(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "naumi").write_bytes(b"backend")
    (backend / "naumi").chmod(0o755)
    ui = tmp_path / "naumi-ui"
    ui.write_bytes(b"ui")
    ui.chmod(0o755)
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    artifact = assemble_release_artifact(
        backend_dir=backend,
        ui_binary=ui,
        config_example=config,
        output_dir=tmp_path / "fixture-release",
        version="1.2.3",
        target="macos-arm64",
        archive_format="tar.gz",
    )
    fixture_dir = tmp_path / "downloads"
    fixture_dir.mkdir()
    stable = fixture_dir / "naumi-macos-arm64.tar.gz"
    stable.write_bytes(artifact.archive.read_bytes())
    digest = hashlib.sha256(stable.read_bytes()).hexdigest()
    (fixture_dir / f"{stable.name}.sha256").write_text(
        f"{digest}  {stable.name}\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "url=''\nout=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = '--output' ]; then out=$2; shift 2; continue; fi\n"
        "  case \"$1\" in https://*) url=$1 ;; esac\n"
        "  shift\n"
        "done\n"
        "cp \"$FIXTURE_DIR/${url##*/}\" \"$out\"\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text(
        "#!/bin/sh\n[ \"${1:-}\" = '-s' ] && echo Darwin || echo arm64\n",
        encoding="utf-8",
    )
    uname.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FIXTURE_DIR": str(fixture_dir),
        "NAUMI_RELEASE_BASE_URL": "https://fixtures.invalid",
        "NAUMI_INSTALL_ROOT": str(tmp_path / "install"),
        "NAUMI_BIN_DIR": str(tmp_path / "bin"),
        "HOME": str(tmp_path / "home"),
    }
    first = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    second = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    command = tmp_path / "bin" / "naumi"
    assert command.is_symlink()
    assert command.resolve().read_bytes() == b"backend"
    assert second.returncode != 0
    assert "该版本已安装" in second.stderr
    assert command.resolve().read_bytes() == b"backend"


def test_readme_declares_terminal_ui_as_default_entry() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "`naumi` 默认启动新一代 Node Terminal UI" in readme
    assert "启动失败时自动回退到 Textual TUI" in readme
    assert "naumi tui" in readme
    assert "naumi chat --classic" not in readme
    assert "Prompt Toolkit 兼容 CLI" not in readme
