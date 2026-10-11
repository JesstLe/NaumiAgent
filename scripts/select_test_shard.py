"""Select a deterministic, size-balanced shard of pytest files."""

from __future__ import annotations

import argparse
from pathlib import Path


def select_test_shards(files: list[Path], shard_count: int) -> list[list[Path]]:
    """Distribute files greedily by source size while keeping output deterministic."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if not files:
        raise ValueError("no test files found")

    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    sizes = [0] * shard_count
    ordered = sorted(files, key=lambda path: (-path.stat().st_size, path.as_posix()))
    for path in ordered:
        shard_index = min(range(shard_count), key=lambda index: (sizes[index], index))
        shards[shard_index].append(path)
        sizes[shard_index] += path.stat().st_size

    for shard in shards:
        shard.sort(key=lambda path: path.as_posix())
    return shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    files = list(args.root.glob("test_*.py"))
    shards = select_test_shards(files, args.shard_count)
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be within the configured shard count")

    selected = shards[args.shard_index]
    args.output.write_text(
        "".join(f"{path.as_posix()}\n" for path in selected),
        encoding="utf-8",
    )
    print(
        f"selected {len(selected)} of {len(files)} files "
        f"for shard {args.shard_index}/{args.shard_count - 1}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
