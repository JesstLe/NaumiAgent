#!/usr/bin/env python3
"""CLI wrapper for Naumi release artifact assembly."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.build_attestations import ReleaseBuildContext, ReleaseBuildSigner


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
    parser.add_argument(
        "--allow-unsigned-development-artifact",
        action="store_true",
        help="仅用于本地开发夹具；正式发行不得使用",
    )
    args = parser.parse_args()
    source_commit, source_tree_sha256 = _source_provenance(args)
    build_signer, build_context = _release_signing(args)
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
        build_signer=build_signer,
        build_context=build_context,
    )
    print(result.archive)
    print(result.checksum)
    if result.attestation is not None:
        print(result.attestation)


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


def _release_signing(args) -> tuple[ReleaseBuildSigner | None, ReleaseBuildContext | None]:
    private_key = os.environ.get("NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64", "").strip()
    if not private_key:
        if args.allow_unsigned_development_artifact:
            return None, None
        raise SystemExit(
            "缺少 NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64；"
            "正式发行必须生成可信构建证明。"
        )
    required = {
        "builder id": os.environ.get("NAUMI_RELEASE_BUILDER_ID", "").strip(),
        "builder key id": os.environ.get("NAUMI_RELEASE_BUILDER_KEY_ID", "").strip(),
        "builder key generation": os.environ.get(
            "NAUMI_RELEASE_BUILDER_KEY_GENERATION", ""
        ).strip(),
        "repository": os.environ.get("GITHUB_REPOSITORY", "").strip(),
        "workflow ref": os.environ.get("GITHUB_WORKFLOW_REF", "").strip(),
        "run id": os.environ.get("GITHUB_RUN_ID", "").strip(),
        "run attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit("构建证明缺少环境字段：" + "、".join(missing))
    try:
        key_generation = int(required["builder key generation"])
        run_attempt = int(required["run attempt"])
        base64.b64decode(private_key, validate=True)
        signer = ReleaseBuildSigner.from_private_key_base64(
            builder_id=required["builder id"],
            key_id=required["builder key id"],
            key_generation=key_generation,
            private_key_base64=private_key,
        )
        context = ReleaseBuildContext(
            repository=required["repository"],
            workflow_ref=required["workflow ref"],
            run_id=required["run id"],
            run_attempt=run_attempt,
            built_at=os.environ.get("NAUMI_RELEASE_BUILT_AT", "").strip()
            or datetime.now(UTC).isoformat(),
        )
    except (TypeError, ValueError) as exc:
        raise SystemExit("构建证明环境字段无效。") from exc
    return signer, context


if __name__ == "__main__":
    main()
