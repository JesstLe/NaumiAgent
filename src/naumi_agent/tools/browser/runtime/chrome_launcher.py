"""Auto-launch Chrome with remote debugging.

Ported from browser-debugging-daemon/scripts/runtime/ChromeLauncher.js.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222
DEFAULT_MAX_PORT_ATTEMPTS = 20
DEFAULT_STALENESS_MS = 3600 * 1000
DEFAULT_LAUNCH_TIMEOUT_MS = 30000
CDP_POLL_INTERVAL_S = 0.5

PROFILE_FILES = [
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    "Preferences",
    "Secure Preferences",
    "Favicons",
    "Favicons-journal",
]

PROFILE_DIRS = [
    "Local Storage",
    "IndexedDB",
    "Session Storage",
]


def _expand_home(path: str) -> Path:
    p = Path(path)
    if p.parts and p.parts[0] == "~":
        return Path.home() / Path(*p.parts[1:])
    return p


class ChromeLauncher:
    def __init__(self, *, cdp_port: int = DEFAULT_CDP_PORT, **_kwargs: Any) -> None:
        env_binary = os.environ.get("BROWSER_CHROME_BINARY", "")
        self.chrome_binary: str | None = env_binary.strip() or None
        self.chrome_profile = os.environ.get("BROWSER_CHROME_PROFILE", "Default")
        raw_dir = os.environ.get(
            "BROWSER_CHROME_DEBUG_DIR", "~/.chrome-debug-profile"
        )
        self.debug_profile_dir = _expand_home(raw_dir)
        self.cdp_port = cdp_port
        self.max_port_attempts = DEFAULT_MAX_PORT_ATTEMPTS
        self.staleness_threshold_ms = DEFAULT_STALENESS_MS
        self._chrome_process: subprocess.Popen[bytes] | None = None

    # -- Platform detection --

    def _detect_platform(self) -> dict[str, str | None]:
        home = Path.home()
        system = platform.system()
        if system == "Darwin":
            return {
                "binary": (
                    "/Applications/Google Chrome.app"
                    "/Contents/MacOS/Google Chrome"
                ),
                "profile_dir": str(
                    home
                    / "Library"
                    / "Application Support"
                    / "Google"
                    / "Chrome"
                ),
            }
        if system == "Linux":
            return {
                "binary": self._find_linux_binary(),
                "profile_dir": str(home / ".config" / "google-chrome"),
            }
        if system == "Windows":
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            local_app = os.environ.get(
                "LOCALAPPDATA", str(home / "AppData" / "Local")
            )
            return {
                "binary": str(
                    Path(program_files)
                    / "Google"
                    / "Chrome"
                    / "Application"
                    / "chrome.exe"
                ),
                "profile_dir": str(
                    Path(local_app) / "Google" / "Chrome" / "User Data"
                ),
            }
        return {"binary": None, "profile_dir": None}

    @staticmethod
    def _find_linux_binary() -> str:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return candidates[0]

    def _resolve_binary(self) -> str:
        if self.chrome_binary:
            if not Path(self.chrome_binary).exists():
                raise FileNotFoundError(
                    f"Chrome binary not found at: {self.chrome_binary}"
                )
            return self.chrome_binary
        info = self._detect_platform()
        binary = info.get("binary")
        if not binary or not Path(binary).exists():
            raise FileNotFoundError(
                f"Chrome not found. Set BROWSER_CHROME_BINARY. "
                f"Searched: {binary or '(none)'}"
            )
        return binary

    def _resolve_source_profile_dir(self) -> Path:
        info = self._detect_platform()
        base = info.get("profile_dir")
        if not base:
            raise RuntimeError("Cannot detect Chrome profile directory.")
        source = Path(base) / self.chrome_profile
        if not source.exists():
            raise FileNotFoundError(f"Chrome profile not found at: {source}")
        return source

    def _resolve_debug_profile_dir(self) -> Path:
        target = self.debug_profile_dir / self.chrome_profile
        target.mkdir(parents=True, exist_ok=True)
        return target

    # -- Profile sync --

    def _is_profile_sync_needed(self, force: bool = False) -> bool:
        if force:
            return True
        target = self.debug_profile_dir / self.chrome_profile
        if not target.exists():
            return True
        cookie_file = target / "Cookies"
        if not cookie_file.exists():
            return True
        age_ms = (
            (asyncio.get_event_loop().time() if False else 0)
            or 0
        )
        import time
        age_ms = time.time() * 1000 - cookie_file.stat().st_mtime * 1000
        return age_ms > self.staleness_threshold_ms

    def _sync_profile(self) -> dict[str, Any]:
        source_dir = self._resolve_source_profile_dir()
        target_dir = self._resolve_debug_profile_dir()
        synced = 0
        errors: list[str] = []

        for name in PROFILE_FILES:
            src = source_dir / name
            dst = target_dir / name
            try:
                if src.exists():
                    shutil.copy2(str(src), str(dst))
                    synced += 1
            except OSError as exc:
                errors.append(f"{name}: {exc}")

        for name in PROFILE_DIRS:
            src = source_dir / name
            dst = target_dir / name
            try:
                if src.exists():
                    if dst.exists():
                        shutil.rmtree(str(dst))
                    shutil.copytree(str(src), str(dst))
                    synced += 1
            except OSError as exc:
                errors.append(f"{name}/: {exc}")

        return {"synced_files": synced, "errors": errors}

    # -- CDP / Port --

    async def _is_cdp_active(self, port: int) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    def _find_available_port(self, start_port: int) -> int:
        for port in range(start_port, start_port + self.max_port_attempts):
            try:
                result = subprocess.run(
                    ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                    capture_output=True,
                    timeout=3,
                )
                if result.returncode != 0:
                    return port
            except Exception:
                return port
        raise RuntimeError(
            f"No available port after {self.max_port_attempts} "
            f"attempts from {start_port}"
        )

    # -- Chrome launch --

    def _launch_chrome(self, port: int) -> None:
        binary = self._resolve_binary()

        lock_file = self.debug_profile_dir / "SingletonLock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except OSError:
                pass

        cmd = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.debug_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
        ]
        self._chrome_process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    async def _wait_for_cdp(
        self, port: int, timeout_ms: int = DEFAULT_LAUNCH_TIMEOUT_MS
    ) -> bool:
        import time

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._is_cdp_active(port):
                return True
            await asyncio.sleep(CDP_POLL_INTERVAL_S)
        raise TimeoutError(
            f"Chrome CDP not ready after {timeout_ms}ms on port {port}"
        )

    # -- Public API --

    async def ensure_ready(
        self, *, force_resync: bool = False
    ) -> dict[str, Any]:
        if await self._is_cdp_active(self.cdp_port):
            return {
                "endpoint": f"http://127.0.0.1:{self.cdp_port}",
                "launched": False,
                "synced": False,
                "port": self.cdp_port,
            }

        synced = False
        if self._is_profile_sync_needed(force_resync):
            result = self._sync_profile()
            synced = True
            if result["errors"]:
                logger.warning("Profile sync warnings: %s", "; ".join(result["errors"]))

        port = self._find_available_port(self.cdp_port)
        self._launch_chrome(port)
        await self._wait_for_cdp(port)

        return {
            "endpoint": f"http://127.0.0.1:{port}",
            "launched": True,
            "synced": synced,
            "port": port,
        }

    def kill_chrome(self) -> dict[str, Any]:
        if self._chrome_process is None or self._chrome_process.poll() is not None:
            return {"killed": False, "pid": None}
        pid = self._chrome_process.pid
        try:
            self._chrome_process.terminate()
        except OSError:
            pass
        self._chrome_process = None
        return {"killed": True, "pid": pid}

    def get_debug_info(self) -> dict[str, Any]:
        info = self._detect_platform()
        return {
            "platform": sys.platform,
            "binary_path": self.chrome_binary or info.get("binary"),
            "source_profile_dir": info.get("profile_dir"),
            "debug_profile_dir": str(self.debug_profile_dir),
            "cdp_port": self.cdp_port,
            "chrome_process_pid": (
                self._chrome_process.pid if self._chrome_process else None
            ),
        }
