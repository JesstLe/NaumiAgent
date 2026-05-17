"""Tests for browser SoM and shared helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from naumi_agent.tools.browser.som import (
    _decrypt_storage_state,
    _encrypt_storage_state,
    get_select_all_shortcut,
    load_storage_state,
    save_browser_state,
    score_candidate,
    text_similarity,
    write_base64_files,
)

# ---------------------------------------------------------------------------
# score_candidate / text_similarity
# ---------------------------------------------------------------------------


class TestTextSimilarity:
    def test_exact_match(self) -> None:
        assert text_similarity("Hello", "Hello") == 1.0

    def test_case_insensitive_exact(self) -> None:
        assert text_similarity("hello", "HELLO") == 1.0

    def test_substring(self) -> None:
        assert text_similarity("Login", "Login Button") == 0.6

    def test_reverse_substring(self) -> None:
        assert text_similarity("Login Button", "Login") == 0.6

    def test_no_match(self) -> None:
        assert text_similarity("foo", "bar") == 0.0

    def test_empty(self) -> None:
        assert text_similarity("", "text") == 0.0

    def test_none_like(self) -> None:
        assert text_similarity(None, "text") == 0.0


class TestScoreCandidate:
    def _make_element(
        self,
        tag: str = "button",
        text: str = "Submit",
        x: float = 100.0,
        y: float = 100.0,
        **extra: Any,
    ) -> dict[str, Any]:
        elem: dict[str, Any] = {"tag": tag, "text": text, "x": x, "y": y}
        elem.update(extra)
        return elem

    def test_tag_match_bonus(self) -> None:
        prev = self._make_element(tag="button")
        same_tag = self._make_element(tag="button")
        diff_tag = self._make_element(tag="a")
        assert score_candidate(prev, same_tag) > score_candidate(prev, diff_tag)

    def test_text_match_bonus(self) -> None:
        prev = self._make_element(text="Login")
        same = self._make_element(text="Login")
        diff = self._make_element(text="Signup")
        assert score_candidate(prev, same) > score_candidate(prev, diff)

    def test_position_penalty(self) -> None:
        prev = self._make_element(x=100, y=100)
        nearby = self._make_element(x=110, y=110)
        far = self._make_element(x=500, y=500)
        assert score_candidate(prev, nearby) > score_candidate(prev, far)

    def test_combined_scoring(self) -> None:
        prev = self._make_element(
            tag="a", text="GitHub", href="https://github.com", x=50, y=50
        )
        perfect = self._make_element(
            tag="a", text="GitHub", href="https://github.com", x=52, y=52
        )
        wrong = self._make_element(
            tag="input", text="Search", x=500, y=500
        )
        score_perfect = score_candidate(prev, perfect)
        score_wrong = score_candidate(prev, wrong)
        assert score_perfect > score_wrong

    def test_role_and_type_bonuses(self) -> None:
        prev = self._make_element(role="button", type="submit")
        match = self._make_element(role="button", type="submit")
        no_match = self._make_element(role="link", type="text")
        assert score_candidate(prev, match) > score_candidate(prev, no_match)


# ---------------------------------------------------------------------------
# Storage state encryption
# ---------------------------------------------------------------------------


class TestStorageStateEncryption:
    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path) -> None:
        original = {"cookies": [{"name": "session", "value": "abc123"}]}
        payload_json = json.dumps(original)
        secret = "test-secret-key"

        encrypted = _encrypt_storage_state(payload_json, secret)
        assert encrypted["__encrypted"] is True
        assert encrypted["alg"] == "aes-256-gcm"
        assert "iv" in encrypted
        assert "tag" in encrypted
        assert "ciphertext" in encrypted

        decrypted = _decrypt_storage_state(encrypted, secret)
        assert decrypted == original

    def test_wrong_secret_fails(self) -> None:
        payload_json = json.dumps({"test": True})
        encrypted = _encrypt_storage_state(payload_json, "correct-secret")
        with pytest.raises(Exception):
            _decrypt_storage_state(encrypted, "wrong-secret")


class TestLoadSaveStorageState:
    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        result = await load_storage_state(tmp_path / "nonexistent.json")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_plaintext(self, tmp_path: Path) -> None:
        state = {"cookies": [{"name": "token", "value": "xyz"}]}
        path = tmp_path / "storage_state.json"
        path.write_text(json.dumps(state), "utf-8")
        result = await load_storage_state(str(path))
        assert result == state

    @pytest.mark.asyncio
    async def test_load_encrypted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        original = {"cookies": [{"name": "secret", "value": "val"}]}
        secret = "my-encryption-key"
        monkeypatch.setenv("BROWSER_STORAGE_STATE_SECRET", secret)

        encrypted = _encrypt_storage_state(json.dumps(original), secret)
        path = tmp_path / "storage_state.json"
        path.write_text(json.dumps(encrypted), "utf-8")

        result = await load_storage_state(str(path))
        assert result == original

    @pytest.mark.asyncio
    async def test_save_plaintext(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BROWSER_STORAGE_STATE_SECRET", raising=False)
        path = tmp_path / "state.json"

        mock_context = AsyncMock()
        mock_context.storage_state.return_value = {"cookies": []}

        result = await save_browser_state(mock_context, str(path))
        assert result is True
        assert path.exists()
        data = json.loads(path.read_text("utf-8"))
        assert "cookies" in data

    @pytest.mark.asyncio
    async def test_save_encrypted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        secret = "save-encryption-key"
        monkeypatch.setenv("BROWSER_STORAGE_STATE_SECRET", secret)
        path = tmp_path / "state_enc.json"

        mock_context = AsyncMock()
        mock_context.storage_state.return_value = {"cookies": [{"name": "a", "value": "b"}]}

        result = await save_browser_state(mock_context, str(path))
        assert result is True
        data = json.loads(path.read_text("utf-8"))
        assert data.get("__encrypted") is True

    @pytest.mark.asyncio
    async def test_save_none_context(self, tmp_path: Path) -> None:
        result = await save_browser_state(None, str(tmp_path / "state.json"))
        assert result is False


# ---------------------------------------------------------------------------
# write_base64_files
# ---------------------------------------------------------------------------


class TestWriteBase64Files:
    def test_writes_file(self) -> None:
        content = base64_b64encode(b"hello world")
        paths = write_base64_files([{"name": "test.txt", "content": content}])
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == b"hello world"

    def test_empty_list(self) -> None:
        assert write_base64_files([]) == []

    def test_invalid_file_raises(self) -> None:
        with pytest.raises(ValueError):
            write_base64_files([{"name": "test.txt"}])

    def test_multiple_files(self) -> None:
        files = [
            {"name": "a.txt", "content": base64_b64encode(b"aaa")},
            {"name": "b.txt", "content": base64_b64encode(b"bbb")},
        ]
        paths = write_base64_files(files)
        assert len(paths) == 2


def base64_b64encode(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# get_select_all_shortcut
# ---------------------------------------------------------------------------


class TestGetSelectAllShortcut:
    def test_returns_string(self) -> None:
        result = get_select_all_shortcut()
        assert result in ("Meta+A", "Control+A")

    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("naumi_agent.tools.browser.som.platform.system", lambda: "Darwin")
        assert get_select_all_shortcut() == "Meta+A"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("naumi_agent.tools.browser.som.platform.system", lambda: "Linux")
        assert get_select_all_shortcut() == "Control+A"
