"""Cross-platform user-owned state locations outside Agent workspaces."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_naumi_state_home() -> Path:
    """Resolve the platform-native Naumi state directory without creating it."""
    explicit = os.environ.get("NAUMI_STATE_HOME", "").strip()
    if explicit:
        state_home = Path(explicit).expanduser()
    elif sys.platform == "darwin":
        state_home = Path.home() / "Library" / "Application Support" / "NaumiAgent"
    elif sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        state_home = (
            Path(local_app_data) / "NaumiAgent"
            if local_app_data
            else Path.home() / "AppData" / "Local" / "NaumiAgent"
        )
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        state_home = (
            Path(xdg_state_home) / "naumi-agent"
            if xdg_state_home
            else Path.home() / ".local" / "state" / "naumi-agent"
        )
    return state_home.resolve()


__all__ = ["resolve_naumi_state_home"]
