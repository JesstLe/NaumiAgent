"""Resolve the naumi-analysis pi extension and interpreter for pi spawns."""

from __future__ import annotations

import sys
from pathlib import Path

EXTENSION_FILENAME = "pi_extensions/naumi-analysis.js"
_EXTENSION_FLAGS = {"-e", "--extension"}


def resolve_default_extension_args(
    workspace_root: Path,
    extra_args: list[str],
) -> list[str]:
    """Auto-load the bundled analysis extension when nothing overrides it.

    Explicit ``-e/--extension`` or ``--no-extensions`` in extra_args wins;
    otherwise, when ``pi_extensions/naumi-analysis.js`` exists under the
    workspace (source checkouts ship it), append it so pi sessions get the
    naumi analysis tools out of the box.
    """
    if any(flag in _EXTENSION_FLAGS for flag in extra_args):
        return list(extra_args)
    if "--no-extensions" in extra_args:
        return list(extra_args)
    candidate = workspace_root / EXTENSION_FILENAME
    if candidate.is_file():
        return [*extra_args, "-e", str(candidate)]
    return list(extra_args)


def default_pi_env(resolved_env: dict[str, str] | None) -> dict[str, str]:
    """Environment overrides every pi spawn should carry.

    ``NAUMI_PYTHON`` points the bundled extension at the interpreter that
    has naumi_agent importable — the very interpreter running NaumiAgent.
    """
    env = {"NAUMI_PYTHON": sys.executable}
    if resolved_env:
        env.update(resolved_env)
    return env
