"""Tests for browser runtime support modules."""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.tools.browser.runtime.artifact_store import (
    ArtifactStore,
    _sanitize_segment,
)
from naumi_agent.tools.browser.runtime.chrome_launcher import (
    ChromeLauncher,
    _expand_home,
)
from naumi_agent.tools.browser.runtime.download_manager import (
    DownloadManager,
    _safe_filename,
)
from naumi_agent.tools.browser.runtime.network_recorder import (
    NetworkRecorder,
    _sanitize_headers,
)

# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


class TestSanitizeSegment:
    def test_basic(self) -> None:
        assert _sanitize_segment("Hello World") == "hello-world"

    def test_special_chars(self) -> None:
        assert _sanitize_segment("foo@bar!baz") == "foo-bar-baz"

    def test_truncation(self) -> None:
        result = _sanitize_segment("a" * 200)
        assert len(result) == 60

    def test_empty_returns_fallback(self) -> None:
        assert _sanitize_segment("") == "artifact"

    def test_leading_trailing_whitespace(self) -> None:
        assert _sanitize_segment("  hello  ") == "hello"


class TestArtifactStore:
    def test_start_session_creates_dirs(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        assert store.session_dir is not None
        assert store.screenshots_dir is not None
        assert store.screenshots_dir.exists()
        assert store.videos_dir is not None
        assert store.videos_dir.exists()
        assert store.traces_dir is not None
        assert store.traces_dir.exists()

    def test_get_step_screenshot_path(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        path = store.get_step_screenshot_path("click")
        assert path.name.startswith("001_")
        assert path.suffix == ".png"
        path2 = store.get_step_screenshot_path("type")
        assert path2.name.startswith("002_")

    def test_get_video_path(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        path = store.get_video_path("test")
        assert path.suffix == ".webm"
        assert "test_" in path.name

    def test_get_trace_path(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        path = store.get_trace_path("session")
        assert path.suffix == ".zip"

    def test_append_event(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        store.append_event("test_event", {"key": "value"})
        assert store.events_path is not None
        lines = store.events_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "test_event"
        assert entry["payload"]["key"] == "value"

    def test_write_json(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        path = store.write_json("data.json", {"a": 1})
        data = json.loads(path.read_text())
        assert data["a"] == 1

    def test_write_text(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        path = store.write_text("notes.txt", "hello")
        assert path.read_text() == "hello"

    def test_get_summary(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.start_session()
        summary = store.get_summary()
        assert summary["sessionId"] is not None
        assert summary["screenshotsDir"] is not None

    def test_cleanup_retained_sessions_max_sessions(
        self, tmp_path: Path
    ) -> None:
        store = ArtifactStore(tmp_path)
        os.environ["BROWSER_ARTIFACT_MAX_SESSIONS"] = "2"
        try:
            store.max_sessions = 2
            for _ in range(4):
                store.start_session()
                time.sleep(0.01)
            store.start_session()
            remaining = list(
                (store.root_dir).iterdir()
            )
            # The new session + up to max_sessions retained
            assert len(remaining) <= store.max_sessions + 1
        finally:
            os.environ.pop("BROWSER_ARTIFACT_MAX_SESSIONS", None)

    def test_active_session_raises_if_no_session(
        self, tmp_path: Path
    ) -> None:
        store = ArtifactStore(tmp_path)
        with pytest.raises(RuntimeError):
            store.get_video_dir()

    def test_get_current_view_path(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        path = store.get_current_view_path()
        assert path.name == "current_view.png"

    def test_list_video_files_empty(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        assert store.list_video_files() == []

    def test_list_trace_files_empty(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        assert store.list_trace_files() == []


# ---------------------------------------------------------------------------
# ChromeLauncher
# ---------------------------------------------------------------------------


class TestExpandHome:
    def test_tilde_expansion(self) -> None:
        result = _expand_home("~/test")
        assert str(result).startswith(str(Path.home()))
        assert "test" in str(result)

    def test_no_tilde(self) -> None:
        result = _expand_home("/absolute/path")
        assert str(result) == "/absolute/path"

    def test_relative_path(self) -> None:
        result = _expand_home("relative/path")
        assert str(result) == "relative/path"


class TestChromeLauncher:
    def test_init_defaults(self) -> None:
        launcher = ChromeLauncher(cdp_port=9222)
        assert launcher.cdp_port == 9222
        assert launcher.chrome_binary is None

    def test_init_env_binary(self) -> None:
        with patch.dict(
            os.environ, {"BROWSER_CHROME_BINARY": "/usr/bin/test-chrome"}
        ):
            launcher = ChromeLauncher()
            assert launcher.chrome_binary == "/usr/bin/test-chrome"

    def test_detect_platform(self) -> None:
        launcher = ChromeLauncher()
        info = launcher._detect_platform()
        system = platform.system()
        if system == "Darwin":
            assert "Chrome" in (info.get("binary") or "")
            assert info.get("profile_dir") is not None
        elif system == "Linux":
            assert info.get("binary") is not None
            assert info.get("profile_dir") is not None

    def test_resolve_binary_env_set(self, tmp_path: Path) -> None:
        fake_chrome = tmp_path / "chrome"
        fake_chrome.write_text("")
        launcher = ChromeLauncher()
        launcher.chrome_binary = str(fake_chrome)
        assert launcher._resolve_binary() == str(fake_chrome)

    def test_resolve_binary_env_missing(self) -> None:
        launcher = ChromeLauncher()
        launcher.chrome_binary = "/nonexistent/chrome"
        with pytest.raises(FileNotFoundError):
            launcher._resolve_binary()

    def test_get_debug_info(self) -> None:
        launcher = ChromeLauncher()
        info = launcher.get_debug_info()
        assert "platform" in info
        assert "cdp_port" in info

    def test_kill_chrome_no_process(self) -> None:
        launcher = ChromeLauncher()
        result = launcher.kill_chrome()
        assert result["killed"] is False


# ---------------------------------------------------------------------------
# DownloadManager
# ---------------------------------------------------------------------------


class TestSafeFilename:
    def test_basic(self) -> None:
        assert _safe_filename("file.txt") == "file.txt"

    def test_special_chars(self) -> None:
        result = _safe_filename("my file (1).pdf")
        assert " " not in result

    def test_truncation(self) -> None:
        result = _safe_filename("a" * 300 + ".txt")
        assert len(result) <= 200

    def test_empty(self) -> None:
        assert _safe_filename("") == "download"

    def test_all_special(self) -> None:
        assert _safe_filename("@#$%") == "____"


class TestDownloadManager:
    def test_init(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        assert dm._downloads_dir.exists()

    def test_attach_none(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        dm.attach(None)  # should not raise

    def test_list_downloads_empty(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        assert dm.list_downloads() == []

    def test_get_not_found(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        assert dm.get("nonexistent") is None

    def test_clear(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        dm._downloads.append({"id": "test"})
        dm.clear()
        assert len(dm._downloads) == 0

    def test_detach(self, tmp_path: Path) -> None:
        dm = DownloadManager(tmp_path)
        dm.detach()  # should not raise


# ---------------------------------------------------------------------------
# NetworkRecorder
# ---------------------------------------------------------------------------


class TestSanitizeHeaders:
    def test_redacts_auth(self) -> None:
        headers = {
            "authorization": "Bearer token123",
            "content-type": "application/json",
        }
        result = _sanitize_headers(headers)
        assert result["authorization"] == "[redacted]"
        assert result["content-type"] == "application/json"

    def test_redacts_cookie(self) -> None:
        headers = {"cookie": "session=abc", "accept": "*/*"}
        result = _sanitize_headers(headers)
        assert result["cookie"] == "[redacted]"
        assert result["accept"] == "*/*"

    def test_redacts_set_cookie(self) -> None:
        headers = {"set-cookie": "id=1", "host": "example.com"}
        result = _sanitize_headers(headers)
        assert result["set-cookie"] == "[redacted]"

    def test_none_input(self) -> None:
        assert _sanitize_headers(None) == {}

    def test_empty_input(self) -> None:
        assert _sanitize_headers({}) == {}


class TestNetworkRecorder:
    def test_init(self) -> None:
        rec = NetworkRecorder()
        assert rec.entries == []
        assert not rec.enabled

    def test_attach_none(self) -> None:
        rec = NetworkRecorder()
        rec.attach(None)  # should not raise

    def test_clear(self) -> None:
        rec = NetworkRecorder()
        rec.entries.append({"type": "test"})
        rec.clear()
        assert len(rec.entries) == 0

    def test_get_summary_empty(self) -> None:
        rec = NetworkRecorder()
        summary = rec.get_summary()
        assert summary["totalRequests"] == 0
        assert summary["totalResponses"] == 0
        assert summary["failed"] == 0

    def test_detach(self) -> None:
        rec = NetworkRecorder()
        rec.enabled = True
        rec.detach()
        assert not rec.enabled

    def test_on_request_disabled(self) -> None:
        rec = NetworkRecorder()
        mock_req = MagicMock()
        rec._on_request(mock_req)
        assert len(rec.entries) == 0

    def test_on_response_disabled(self) -> None:
        rec = NetworkRecorder()
        mock_resp = MagicMock()
        rec._on_response(mock_resp)
        assert len(rec.entries) == 0

    def test_on_request_failed_disabled(self) -> None:
        rec = NetworkRecorder()
        mock_req = MagicMock()
        rec._on_request_failed(mock_req)
        assert len(rec.entries) == 0

    def test_max_entries_eviction(self) -> None:
        rec = NetworkRecorder(max_entries=5)
        rec.enabled = True
        for i in range(10):
            mock_req = MagicMock()
            mock_req.url = f"http://example.com/{i}"
            mock_req.method = "GET"
            mock_req.resource_type = "document"
            mock_req.headers = {}
            mock_req.post_data = None
            rec._on_request(mock_req)
        assert len(rec.entries) == 5


# ---------------------------------------------------------------------------
# BrowserRuntime (unit-level, no real browser)
# ---------------------------------------------------------------------------


class TestBrowserRuntimeInit:
    def test_import(self) -> None:
        pass

    def test_init(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        assert rt.browser_mode == "stopped"
        assert rt.requested_source == "auto"
        assert rt.session_source == "managed"
        assert rt.browser is None
        assert rt.context is None
        assert rt.page is None

    def test_is_running_false(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        assert not rt.is_running()

    def test_current_session_source_none(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        assert rt.current_session_source() is None

    def test_get_debug_state_stopped(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        state = rt.get_debug_state()
        assert not state["active"]
        assert state["browserMode"] == "stopped"

    def test_record_event_no_session(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        rt.record_event("test")  # should not raise

    def test_get_chrome_launcher_info(self, tmp_path: Path) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        info = rt.get_chrome_launcher_info()
        assert "platform" in info

    @pytest.mark.asyncio
    async def test_managed_launch_uses_python_playwright_video_options(
        self, tmp_path: Path,
    ) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            BrowserRuntime,
        )

        rt = BrowserRuntime(tmp_path)
        rt.artifacts.start_session()
        fake_page = MagicMock()
        fake_context = MagicMock()
        fake_context.tracing.start = AsyncMock()
        fake_context.new_page = AsyncMock(return_value=fake_page)
        fake_context.add_init_script = AsyncMock()
        fake_browser = MagicMock()
        fake_browser.new_context = AsyncMock(return_value=fake_context)
        fake_chromium = MagicMock()
        fake_chromium.launch = AsyncMock(return_value=fake_browser)
        rt._playwright = MagicMock(chromium=fake_chromium)

        await rt._launch_browser_session(headless=True)

        context_kwargs = fake_browser.new_context.await_args.kwargs
        assert "record_video" not in context_kwargs
        assert context_kwargs["record_video_dir"]
        assert context_kwargs["record_video_size"] == {"width": 1280, "height": 800}
        fake_context.add_init_script.assert_awaited_once()


class TestNormalizeBrowserSource:
    def test_auto(self) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            _normalize_browser_source,
        )

        assert _normalize_browser_source(None) == "auto"
        assert _normalize_browser_source("") == "auto"
        assert _normalize_browser_source("unknown") == "auto"

    def test_valid_sources(self) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            _normalize_browser_source,
        )

        assert _normalize_browser_source("managed") == "managed"
        assert _normalize_browser_source("attached") == "attached"
        assert _normalize_browser_source("AUTO") == "auto"
        assert _normalize_browser_source(" Auto ") == "auto"


class TestTrimLogBuffer:
    def test_within_limit(self) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            _trim_log_buffer,
        )

        buf = [1, 2, 3]
        assert _trim_log_buffer(buf) == [1, 2, 3]

    def test_over_limit(self) -> None:
        from naumi_agent.tools.browser.runtime.browser_runtime import (
            _trim_log_buffer,
        )

        buf = list(range(300))
        result = _trim_log_buffer(buf)
        assert len(result) == 200
        assert result[0] == 100
