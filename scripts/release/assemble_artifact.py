#!/usr/bin/env python3
"""CLI wrapper for Naumi release artifact assembly."""

from __future__ import annotations

import argparse
from pathlib import Path

from naumi_agent.release.artifact import assemble_release_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble one source-free Naumi artifact")
    parser.add_argument("--backend-dir", type=Path, required=True)
    parser.add_argument("--ui-binary", type=Path, required=True)
    parser.add_argument("--config-example", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--archive-format", choices=("tar.gz", "zip"), required=True)
    args = parser.parse_args()
    result = assemble_release_artifact(
        backend_dir=args.backend_dir,
        ui_binary=args.ui_binary,
        config_example=args.config_example,
        output_dir=args.output_dir,
        version=args.version,
        target=args.target,
        archive_format=args.archive_format,
    )
    print(result.archive)
    print(result.checksum)


if __name__ == "__main__":
    main()
