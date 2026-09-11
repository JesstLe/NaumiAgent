"""Shared helpers for pi engine configuration."""

from __future__ import annotations

import os
import re

_ENV_REF = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_env_refs(env: dict[str, str]) -> dict[str, str]:
    """Expand ``{env:NAME}`` references; drop entries whose source is unset."""
    resolved: dict[str, str] = {}
    for key, value in env.items():
        match = _ENV_REF.fullmatch(str(value).strip())
        if match:
            source = os.environ.get(match.group(1))
            if source:
                resolved[key] = source
        else:
            resolved[key] = str(value)
    return resolved
