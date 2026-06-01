"""Automatic memory extraction tests."""

from __future__ import annotations

from naumi_agent.memory.auto_extract import extract_memory_candidates


class TestAutoMemoryExtraction:
    def test_extracts_user_preference(self) -> None:
        candidates = extract_memory_candidates("以后请优先用中文回复，并保持简洁。")

        assert candidates
        assert candidates[0].category == "preference"
        assert "用户偏好" in candidates[0].content
        assert "优先用中文回复" in candidates[0].content

    def test_extracts_project_fact_and_decision(self) -> None:
        candidates = extract_memory_candidates(
            "项目使用 FastAPI 和 SQLite。我们决定采用 worktree 隔离开发。"
        )

        categories = {candidate.category for candidate in candidates}
        contents = "\n".join(candidate.content for candidate in candidates)
        assert "fact" in categories
        assert "decision" in categories
        assert "FastAPI" in contents
        assert "worktree" in contents

    def test_ignores_low_signal_text(self) -> None:
        assert extract_memory_candidates("继续") == []
