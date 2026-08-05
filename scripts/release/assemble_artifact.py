#!/usr/bin/env python3
"""CLI wrapper for Naumi release artifact assembly."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

from naumi_agent.release.artifact import assemble_release_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble one source-free Naumi artifact")
    parser.add_argument("--backend-dir", type=Path, required=True)
    parser.add_argument("--launcher-dir", type=Path, required=True)
    parser.add_argument("--ui-binary", type=Path, required=True)
    parser.add_argument("--config-example", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--source-tree-sha256")
    parser.add_argument("--archive-format", choices=("tar.gz", "zip"), required=True)
    args = parser.parse_args()
    source_commit, source_tree_sha256 = _source_provenance(args)
    result = assemble_release_artifact(
        backend_dir=args.backend_dir,
        launcher_dir=args.launcher_dir,
        ui_binary=args.ui_binary,
        config_example=args.config_example,
        output_dir=args.output_dir,
        version=args.version,
        target=args.target,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
        archive_format=args.archive_format,
    )
    print(result.archive)
    print(result.checksum)


def _source_provenance(args) -> tuple[str, str]:
    if bool(args.source_commit) is not bool(args.source_tree_sha256):
        raise SystemExit("source commit/tree 参数必须同时提供。")
    if args.source_commit:
        return args.source_commit, args.source_tree_sha256
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            .stdout.decode("ascii")
            .strip()
            .casefold()
        )
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--full-tree", commit],
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise SystemExit("无法读取发行源码的 exact Git provenance。") from exc
    return commit, hashlib.sha256(listing).hexdigest()


if __name__ == "__main__":
    main()
