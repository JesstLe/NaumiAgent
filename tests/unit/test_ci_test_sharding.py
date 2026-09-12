from pathlib import Path

import pytest

from scripts.select_test_shard import select_test_shards


def _test_file(root: Path, name: str, size: int) -> Path:
    path = root / name
    path.write_bytes(b"x" * size)
    return path


def test_select_test_shards_is_complete_deterministic_and_balanced(tmp_path: Path) -> None:
    files = [
        _test_file(tmp_path, "test_large.py", 100),
        _test_file(tmp_path, "test_medium.py", 60),
        _test_file(tmp_path, "test_small_a.py", 40),
        _test_file(tmp_path, "test_small_b.py", 20),
    ]

    first = select_test_shards(files, 2)
    second = select_test_shards(list(reversed(files)), 2)

    assert first == second
    assert sorted(path for shard in first for path in shard) == sorted(files)
    shard_sizes = [sum(path.stat().st_size for path in shard) for shard in first]
    assert shard_sizes == [120, 100]


def test_select_test_shards_rejects_invalid_input(tmp_path: Path) -> None:
    file = _test_file(tmp_path, "test_one.py", 1)

    with pytest.raises(ValueError, match="at least 1"):
        select_test_shards([file], 0)
    with pytest.raises(ValueError, match="no test files"):
        select_test_shards([], 1)
