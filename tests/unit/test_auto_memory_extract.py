"""Automatic memory extraction tests."""

from __future__ import annotations

import pytest

from naumi_agent.memory.auto_extract import extract_memory_candidates


class TestAutoMemoryExtraction:
    def test_extracts_user_preference(self) -> None:
        candidates = extract_memory_candidates("以后请优先用中文回复，并保持简洁。")

        assert candidates
        assert candidates[0].category == "preference"
        assert "用户偏好" in candidates[0].content
        assert "优先用中文回复" in candidates[0].content
        assert candidates[0].source_role == "user"
        assert candidates[0].source_quote == "以后请优先用中文回复，并保持简洁"

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

    def test_assistant_preference_is_attributed_suggestion_not_user_preference(self) -> None:
        candidates = extract_memory_candidates("继续", "我喜欢青色界面。")
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.source_role == "assistant"
        assert candidate.source_quote == "我喜欢青色界面"
        assert candidate.content == "助手建议：青色界面"
        assert "用户偏好" not in candidate.content

    def test_same_words_in_different_sources_are_not_deduplicated(self) -> None:
        candidates = extract_memory_candidates("我喜欢黑白界面。", "我喜欢黑白界面。")
        assert [c.source_role for c in candidates] == ["user", "assistant"]
        assert all(c.source_quote == "我喜欢黑白界面" for c in candidates)

    def test_source_boundaries_do_not_complete_each_others_claims(self) -> None:
        assert extract_memory_candidates("我喜欢", "黑白界面。") == []
        assert extract_memory_candidates("the project uses", "SQLite.") == []

    def test_assistant_decision_and_fact_remain_assistant_statements(self) -> None:
        candidates = extract_memory_candidates(
            "继续", "我们决定采用 SQLite。项目使用 FastAPI。"
        )
        assert {c.category for c in candidates} == {"decision", "fact"}
        assert all(c.source_role == "assistant" for c in candidates)
        assert all(c.content.startswith("助手") for c in candidates)
        assert all(c.source_quote in "我们决定采用 SQLite。项目使用 FastAPI。" for c in candidates)

    @pytest.mark.parametrize("message", [
        "他写道：“我喜欢青色界面。”",
        'Someone wrote: "I prefer cyan interfaces."',
        "他说：‘项目使用 MySQL。’",
        "Someone wrote: 'this project uses MySQL.'",
        "> 我喜欢青色界面。",
        "```text\n我喜欢青色界面。\n```",
        "~~~text\n项目使用 MySQL。\n~~~",
        "示例代码 `我喜欢青色界面`",
        "“我喜欢青色界面。",
        "```\n我喜欢青色界面。",
    ])
    def test_quoted_and_code_examples_are_not_personal_assertions(self, message) -> None:
        assert extract_memory_candidates(message) == []
        assert extract_memory_candidates("", message) == []

    @pytest.mark.parametrize("message", [
        "我不喜欢青色界面。",
        "并不是我喜欢青色界面。",
        "不要记住我喜欢青色界面。",
        "我们没有决定采用 MySQL。",
        "我们尚未选择 MySQL。",
        "不要采用 MySQL。",
        "We did not make a decision: use MySQL.",
        "This is not true: the project uses MySQL.",
        "If I prefer cyan interfaces, please ask again.",
        "假设我喜欢青色界面。",
    ])
    def test_negated_and_hypothetical_statements_are_not_positive_memories(self, message) -> None:
        assert extract_memory_candidates(message) == []

    def test_direct_negative_preference_keeps_its_actual_meaning(self) -> None:
        candidates = extract_memory_candidates("我希望不要使用青色界面。")
        assert len(candidates) == 1
        assert candidates[0].content == "用户偏好：不要使用青色界面"
        assert candidates[0].source_quote == "我希望不要使用青色界面"

    def test_quoted_first_claim_does_not_hide_later_direct_assertion(self) -> None:
        candidates = extract_memory_candidates('“我喜欢青色”。我喜欢“黑白界面”。')
        assert len(candidates) == 1
        assert candidates[0].content == "用户偏好：“黑白界面”"
        assert candidates[0].source_quote == "我喜欢“黑白界面”"

    def test_source_quotes_keep_original_whitespace_and_never_use_normalized_copy(self) -> None:
        message = "  I prefer   black and white interfaces."
        candidate = extract_memory_candidates(message)[0]
        assert candidate.content == "用户偏好：black and white interfaces"
        assert candidate.source_quote == "I prefer   black and white interfaces"
        assert candidate.source_quote in message

    def test_candidates_are_bounded_user_first_and_not_shared_mutable_state(self) -> None:
        candidates = extract_memory_candidates(
            "我喜欢中文回复。我希望保持简洁。项目使用 SQLite。",
            "我喜欢英文回复。项目使用 MySQL。",
        )
        assert len(candidates) == 3
        assert all(c.source_role == "user" for c in candidates)
        assert extract_memory_candidates("继续") == []

    @pytest.mark.parametrize("user,assistant", [(None, ""), ("", []), (123, "")])
    def test_non_text_source_does_not_acquire_a_fabricated_role(self, user, assistant) -> None:
        with pytest.raises(TypeError, match="文本"):
            extract_memory_candidates(user, assistant)
