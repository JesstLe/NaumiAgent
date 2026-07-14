"""Deterministic project configuration path resolution."""

from __future__ import annotations

from pathlib import Path

DEFAULT_CONFIG_PATH = ".naumi/config.yaml"
LEGACY_CONFIG_PATH = "config.yaml"


def resolve_config_path(
    path: str | Path,
    *,
    cwd: Path | None = None,
) -> str:
    """Resolve one explicit path or discover the active project config.

    The default performs two ancestor walks: all modern locations first, then
    legacy root files. Any other input is explicit and never falls back.
    """
    start = (cwd or Path.cwd()).expanduser().resolve()
    requested = Path(path).expanduser()
    if str(path) != DEFAULT_CONFIG_PATH:
        candidate = requested if requested.is_absolute() else start / requested
        return str(candidate.resolve())

    directories = (start, *start.parents)
    for relative in (Path(DEFAULT_CONFIG_PATH), Path(LEGACY_CONFIG_PATH)):
        for directory in directories:
            candidate = directory / relative
            if candidate.is_file():
                return str(candidate.resolve())
    return str((start / DEFAULT_CONFIG_PATH).resolve())
