"""Self-modification tool tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from naumi_agent.tools.self_modify import (
    SelfModifyTool,
    _compute_diff,
    _create_git_backup,
    _find_agent_source_dir,
    _find_test_file,
    _is_modifiable_file,
    _is_protected_file,
    _resolve_target_path,
    _rollback_file,
    _run_import_test,
    _run_ruff,
    _run_ruff_format,
    _run_tests,
    validate_and_apply,
)

SOURCE_DIR = _find_agent_source_dir()


class TestFindAgentSourceDir:
    def test_locates_source_dir(self):
        path = _find_agent_source_dir()
        assert Path(path).is_dir()
        assert Path(path).name == "naumi_agent"
        assert (Path(path) / "__init__.py").exists()

    def test_contains_key_directories(self):
        path = _find_agent_source_dir()
        assert (path / "tools").is_dir()
        assert (path / "memory").is_dir()
        assert (path / "orchestrator").is_dir()


class TestResolveTargetPath:
    def test_resolves_relative_path(self):
        path = _resolve_target_path("tools/analysis.py")
        assert path.name == "analysis.py"
        assert "tools" in str(path)

    def test_rejects_non_python(self):
        with pytest.raises(ValueError, match="只支持修改 .py"):
            _resolve_target_path("tools/README.md")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="路径越界"):
            _resolve_target_path("../../../etc/passwd")


class TestIsProtectedFile:
    def test_protects_engine(self):
        assert _is_protected_file(SOURCE_DIR / "orchestrator" / "engine.py")

    def test_protects_hotreload(self):
        assert _is_protected_file(SOURCE_DIR / "tools" / "hotreload.py")

    def test_protects_self_modify(self):
        assert _is_protected_file(SOURCE_DIR / "tools" / "self_modify.py")

    def test_protects_safety(self):
        assert _is_protected_file(SOURCE_DIR / "safety" / "behavior.py")

    def test_protects_config(self):
        assert _is_protected_file(SOURCE_DIR / "config" / "settings.py")

    def test_protects_base_tool(self):
        assert _is_protected_file(SOURCE_DIR / "tools" / "base.py")

    def test_allows_analysis(self):
        assert not _is_protected_file(SOURCE_DIR / "tools" / "analysis.py")

    def test_allows_memory(self):
        assert not _is_protected_file(SOURCE_DIR / "memory" / "long_term.py")


class TestIsModifiableFile:
    def test_tools_are_modifiable(self):
        assert _is_modifiable_file(SOURCE_DIR / "tools" / "analysis.py")

    def test_memory_is_modifiable(self):
        assert _is_modifiable_file(SOURCE_DIR / "memory" / "long_term.py")

    def test_skills_are_modifiable(self):
        assert _is_modifiable_file(SOURCE_DIR / "skills" / "skill.py")

    def test_engine_is_not_modifiable(self):
        assert not _is_modifiable_file(SOURCE_DIR / "orchestrator" / "engine.py")

    def test_config_is_not_modifiable(self):
        assert not _is_modifiable_file(SOURCE_DIR / "config" / "settings.py")


class TestComputeDiff:
    def test_shows_additions(self):
        original = "line1\nline2\n"
        modified = "line1\nline2\nline3\n"
        diff = _compute_diff(original, modified)
        assert "+line3" in diff

    def test_shows_deletions(self):
        original = "line1\nline2\nline3\n"
        modified = "line1\nline3\n"
        diff = _compute_diff(original, modified)
        assert "-line2" in diff

    def test_no_changes(self):
        original = "line1\nline2\n"
        diff = _compute_diff(original, original)
        assert diff == "(无变更)"


class TestRunRuff:
    def test_passes_on_valid_code(self, tmp_path: Path):
        f = tmp_path / "valid.py"
        f.write_text('x = 1\n', encoding="utf-8")
        passed, output = _run_ruff(f)
        assert passed

    def test_fails_on_invalid_code(self, tmp_path: Path):
        f = tmp_path / "invalid.py"
        f.write_text('import os\nimport os\n', encoding="utf-8")
        passed, output = _run_ruff(f)
        assert not passed

    def test_handles_timeout(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text('x = 1\n', encoding="utf-8")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ruff", 30)):
            passed, output = _run_ruff(f)
        assert not passed
        assert "timed out" in output.lower() or "TimeoutExpired" in output


class TestRunRuffFormat:
    def test_passes_on_formatted_code(self, tmp_path: Path):
        f = tmp_path / "fmt.py"
        f.write_text('x = 1\n', encoding="utf-8")
        passed, output = _run_ruff_format(f)
        assert passed


class TestRunImportTest:
    def test_passes_on_valid_module(self):
        file_path = SOURCE_DIR / "tools" / "builtin.py"
        passed, output = _run_import_test(file_path)
        assert passed

    def test_fails_on_invalid_syntax(self, tmp_path: Path):
        f = tmp_path / "bad.py"
        f.write_text('def broken(\n', encoding="utf-8")
        passed, output = _run_import_test(f)
        assert not passed


class TestFindTestFile:
    def test_finds_existing_test_file(self):
        src = SOURCE_DIR / "tools" / "hotreload.py"
        test_file = _find_test_file(src)
        assert test_file is not None
        assert "test_hotreload" in str(test_file)

    def test_returns_none_for_missing_test(self):
        src = SOURCE_DIR / "tools" / "nonexistent_module.py"
        test_file = _find_test_file(src)
        assert test_file is None


class TestRunTests:
    def test_runs_test_file(self):
        src = SOURCE_DIR / "tools" / "hotreload.py"
        passed, output = _run_tests(src)
        # hotreload has tests that should pass (minus skipped ones)
        assert isinstance(passed, bool)
        assert isinstance(output, str)

    def test_handles_missing_test_file(self):
        src = SOURCE_DIR / "tools" / "nonexistent.py"
        passed, output = _run_tests(src)
        assert passed  # No test file → skip → consider passed


class TestCreateGitBackup:
    def test_backup_success(self, tmp_path: Path):
        source = tmp_path / "file.py"
        source.write_text("x = 1\n", encoding="utf-8")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
            ref = _create_git_backup(source)
            assert ref == "blob:abc123"
            assert mock_run.call_args.args[0] == ["git", "hash-object", "-w", "--stdin"]
            assert mock_run.call_args.kwargs["input"] == "x = 1\n"

    def test_backup_failure(self):
        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            ref = _create_git_backup(Path("some/file.py"))
            assert ref is None


class TestRollbackFile:
    def test_rollback_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _rollback_file(Path("some/file.py"))

    def test_rollback_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert not _rollback_file(Path("some/file.py"))


class TestValidateAndApply:
    """Integration tests — uses temp files to avoid modifying real source."""

    def _create_modifiable_file(self, tmp_path: Path) -> Path:
        """Create a fake modifiable file in a temp tools/ directory."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        f = tools_dir / "dummy_tool.py"
        f.write_text('"""Dummy tool."""\n\nx = 1\n', encoding="utf-8")
        return f

    def test_rejects_protected_file(self):
        result = validate_and_apply(
            "orchestrator/engine.py",
            "bad code",
            "trying to break things",
        )
        assert result["status"] == "rejected"
        assert "保护区" in result["error"]

    def test_rejects_non_modifiable_file(self):
        result = validate_and_apply(
            "config/settings.py",
            "bad code",
            "trying to break config",
        )
        assert result["status"] == "rejected"

    def test_rejects_nonexistent_file(self):
        result = validate_and_apply(
            "tools/nonexistent_xyz.py",
            "some code",
            "no file",
        )
        assert result["status"] == "rejected"
        assert "不存在" in result["error"]

    def test_noop_when_content_unchanged(self, tmp_path: Path):
        src_file = self._create_modifiable_file(tmp_path)
        original = src_file.read_text(encoding="utf-8")

        with (
            patch(
                "naumi_agent.tools.self_modify._find_agent_source_dir",
                return_value=tmp_path,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_modifiable_file",
                return_value=True,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_protected_file",
                return_value=False,
            ),
        ):
            result = validate_and_apply(
                "tools/dummy_tool.py",
                original,
                "no change",
            )
        assert result["status"] == "noop"

    def test_rejects_invalid_syntax(self, tmp_path: Path):
        self._create_modifiable_file(tmp_path)

        with (
            patch(
                "naumi_agent.tools.self_modify._find_agent_source_dir",
                return_value=tmp_path,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_modifiable_file",
                return_value=True,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_protected_file",
                return_value=False,
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff",
                return_value=(False, "F841 local variable assigned but never used"),
            ),
        ):
            result = validate_and_apply(
                "tools/dummy_tool.py",
                "bad syntax here(",
                "broken code",
            )
        assert result["status"] == "rejected"
        assert "ruff" in result["error"].lower()

    def test_rolls_back_on_test_failure(self, tmp_path: Path):
        src_file = self._create_modifiable_file(tmp_path)
        original = src_file.read_text(encoding="utf-8")
        new_content = '"""Dummy tool."""\n\nx = 2\n'

        with (
            patch(
                "naumi_agent.tools.self_modify._find_agent_source_dir",
                return_value=tmp_path,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_modifiable_file",
                return_value=True,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_protected_file",
                return_value=False,
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff_format",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_import_test",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._create_git_backup",
                return_value="stash@{0}",
            ),
            patch(
                "naumi_agent.tools.self_modify._run_tests",
                return_value=(False, "1 FAILED"),
            ),
            patch(
                "naumi_agent.tools.self_modify._rollback_file",
                return_value=True,
            ),
        ):
            result = validate_and_apply(
                "tools/dummy_tool.py",
                new_content,
                "change x to 2",
            )
        assert result["status"] == "rolled_back"
        assert result["rollback_success"] is True
        assert src_file.read_text(encoding="utf-8") == original

    def test_rolls_back_on_import_failure(self, tmp_path: Path):
        src_file = self._create_modifiable_file(tmp_path)
        original = src_file.read_text(encoding="utf-8")
        new_content = '"""Dummy tool."""\n\nx = 2\n'

        with (
            patch(
                "naumi_agent.tools.self_modify._find_agent_source_dir",
                return_value=tmp_path,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_modifiable_file",
                return_value=True,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_protected_file",
                return_value=False,
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff_format",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_import_test",
                return_value=(False, "ImportError: boom"),
            ),
            patch(
                "naumi_agent.tools.self_modify._create_git_backup",
                return_value="stash@{0}",
            ),
        ):
            result = validate_and_apply(
                "tools/dummy_tool.py",
                new_content,
                "change x to 2",
            )

        assert result["status"] == "rolled_back"
        assert result["rollback_success"] is True
        assert result["validation"]["import_test"]["passed"] is False
        assert src_file.read_text(encoding="utf-8") == original

    def test_applies_when_all_pass(self, tmp_path: Path):
        self._create_modifiable_file(tmp_path)
        new_content = '"""Dummy tool."""\n\nx = 2\n'

        with (
            patch(
                "naumi_agent.tools.self_modify._find_agent_source_dir",
                return_value=tmp_path,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_modifiable_file",
                return_value=True,
            ),
            patch(
                "naumi_agent.tools.self_modify._is_protected_file",
                return_value=False,
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_ruff_format",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._run_import_test",
                return_value=(True, ""),
            ),
            patch(
                "naumi_agent.tools.self_modify._create_git_backup",
                return_value="stash@{0}",
            ),
            patch(
                "naumi_agent.tools.self_modify._run_tests",
                return_value=(True, "3 passed"),
            ),
        ):
            result = validate_and_apply(
                "tools/dummy_tool.py",
                new_content,
                "change x to 2",
            )
        assert result["status"] == "applied"
        assert result["file"] == "tools/dummy_tool.py"
        assert "+x = 2" in result["diff"]
        assert result["validation"]["ruff_check"]["passed"] is True
        assert result["validation"]["pytest"]["passed"] is True


class TestSelfModifyTool:
    def test_tool_name(self):
        assert SelfModifyTool().name == "self_modify"

    def test_tool_description(self):
        desc = SelfModifyTool().description
        assert "修改" in desc or "自我" in desc

    def test_tool_schema(self):
        schema = SelfModifyTool().parameters_schema
        assert "target_file" in schema["properties"]
        assert "new_content" in schema["properties"]
        assert "description" in schema["properties"]
        assert len(schema["required"]) == 3

    @pytest.mark.asyncio
    async def test_execute_reports_applied(self):
        tool = SelfModifyTool()
        mock_result = {
            "status": "applied",
            "file": "tools/analysis.py",
            "description": "test change",
            "diff": "+new line",
            "validation": {
                "ruff_check": {"passed": True, "output": ""},
                "ruff_format": {"passed": True, "output": ""},
                "import_test": {"passed": True, "output": ""},
                "pytest": {"passed": True, "output": "3 passed"},
            },
            "backup_ref": "stash@{0}",
        }
        with patch(
            "naumi_agent.tools.self_modify.validate_and_apply",
            return_value=mock_result,
        ):
            result = await tool.execute(
                target_file="tools/analysis.py",
                new_content="x = 2",
                description="test change",
            )
        assert "已应用" in result
        assert "tools/analysis.py" in result

    @pytest.mark.asyncio
    async def test_execute_reports_rejected(self):
        tool = SelfModifyTool()
        mock_result = {
            "status": "rejected",
            "file": "orchestrator/engine.py",
            "error": "文件在保护区内",
        }
        with patch(
            "naumi_agent.tools.self_modify.validate_and_apply",
            return_value=mock_result,
        ):
            result = await tool.execute(
                target_file="orchestrator/engine.py",
                new_content="bad",
                description="hack",
            )
        assert "拒绝" in result
        assert "保护区" in result

    @pytest.mark.asyncio
    async def test_execute_reports_rolled_back(self):
        tool = SelfModifyTool()
        mock_result = {
            "status": "rolled_back",
            "file": "tools/analysis.py",
            "error": "测试未通过",
            "rollback_success": True,
            "validation": {
                "ruff_check": {"passed": True, "output": ""},
                "pytest": {"passed": False, "output": "1 FAILED"},
            },
            "diff": "-old\n+new",
        }
        with patch(
            "naumi_agent.tools.self_modify.validate_and_apply",
            return_value=mock_result,
        ):
            result = await tool.execute(
                target_file="tools/analysis.py",
                new_content="x = 2",
                description="broke tests",
            )
        assert "回滚" in result
        assert "成功" in result

    @pytest.mark.asyncio
    async def test_execute_reports_noop(self):
        tool = SelfModifyTool()
        mock_result = {
            "status": "noop",
            "file": "tools/analysis.py",
            "message": "内容无变更",
        }
        with patch(
            "naumi_agent.tools.self_modify.validate_and_apply",
            return_value=mock_result,
        ):
            result = await tool.execute(
                target_file="tools/analysis.py",
                new_content="same",
                description="no change",
            )
        assert "无变更" in result
