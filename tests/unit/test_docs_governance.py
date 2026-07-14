from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_docs import main, validate_repository


def _write_manifest(root: Path, rules: list[dict[str, object]]) -> Path:
    manifest = root / "docs" / "governance.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"version": 1, "rules": rules}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_valid_repository_reports_classification_counts(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# 项目\n\n[使用说明](docs/guide.md)\n")
    _write(tmp_path, "docs/guide.md", "# 使用说明\n")
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "pattern": "README.md",
                "status": "current",
                "enforce_current": True,
            },
            {
                "pattern": "docs/guide.md",
                "status": "reference",
            },
        ],
    )

    report = validate_repository(tmp_path, manifest)

    assert report.errors == ()
    assert report.document_count == 2
    assert report.status_counts == {"current": 1, "reference": 1}


def test_unclassified_document_is_an_actionable_error(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# 项目\n")
    _write(tmp_path, "docs/orphan.md", "# 无归属\n")
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "current", "enforce_current": True}],
    )

    report = validate_repository(tmp_path, manifest)

    assert any("docs/orphan.md" in error and "未分类" in error for error in report.errors)


def test_overlapping_rules_fail_instead_of_using_rule_order(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# 项目\n")
    manifest = _write_manifest(
        tmp_path,
        [
            {"pattern": "*.md", "status": "reference"},
            {"pattern": "README.md", "status": "current", "enforce_current": True},
        ],
    )

    report = validate_repository(tmp_path, manifest)

    assert any("README.md" in error and "多个分类规则" in error for error in report.errors)


def test_missing_local_links_and_images_are_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "# 项目\n\n[缺失文档](docs/missing.md#section)\n\n![缺失图片](docs/missing%20image.png)\n",
    )
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "current", "enforce_current": True}],
    )

    report = validate_repository(tmp_path, manifest)

    assert any("docs/missing.md" in error for error in report.errors)
    assert any("docs/missing image.png" in error for error in report.errors)


def test_encoded_angle_and_reference_local_links_resolve(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        """# 项目

[带空格的文档](<docs/with space.md#section>)
![编码图片](docs/image%20one.png?raw=1)
[参考式链接][guide]

[guide]: docs/guide.md#intro
""",
    )
    _write(tmp_path, "docs/with space.md", "# Section\n")
    _write(tmp_path, "docs/image one.png", "not-a-real-png")
    _write(tmp_path, "docs/guide.md", "# Intro\n")
    manifest = _write_manifest(
        tmp_path,
        [
            {"pattern": "README.md", "status": "current"},
            {"pattern": "docs/*.md", "status": "reference"},
        ],
    )

    report = validate_repository(tmp_path, manifest)

    assert report.errors == ()


def test_external_anchor_and_fenced_example_links_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        """# 项目

[站点](https://example.com/docs)
[邮件](mailto:hello@example.com)
[章节](#section)

行内语法示例：`[label](docs/not-real-inline.md)`。

```markdown
[示例](docs/not-real.md)
```
""",
    )
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "current", "enforce_current": True}],
    )

    report = validate_repository(tmp_path, manifest)

    assert report.errors == ()


@pytest.mark.parametrize(
    "retired_command",
    [
        "请运行 `naumi chat --classic`。",
        "请运行 `python -m naumi_agent.main chat`。",
        "```bash\nnaumi ui --legacy\n```",
    ],
)
def test_retired_public_commands_fail_in_current_documents(
    tmp_path: Path,
    retired_command: str,
) -> None:
    _write(tmp_path, "README.md", f"# 项目\n\n{retired_command}\n")
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "current", "enforce_current": True}],
    )

    report = validate_repository(tmp_path, manifest)

    assert any("退役入口" in error for error in report.errors)


def test_historical_documents_preserve_retired_commands(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# 历史记录\n\n`naumi chat --classic`\n")
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "historical"}],
    )

    report = validate_repository(tmp_path, manifest)

    assert report.errors == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"version": 2, "rules": []}, "version"),
        ({"version": 1, "rules": []}, "rules"),
        (
            {"version": 1, "rules": [{"pattern": "README.md", "status": "future"}]},
            "status",
        ),
        (
            {
                "version": 1,
                "rules": [
                    {
                        "pattern": "README.md",
                        "status": "current",
                        "enforce_current": "yes",
                    }
                ],
            },
            "enforce_current",
        ),
        (
            {
                "version": 1,
                "rules": [
                    {
                        "pattern": "README.md",
                        "status": "current",
                        "enforce_current": False,
                    }
                ],
            },
            "enforce_current",
        ),
    ],
)
def test_invalid_manifest_has_chinese_actionable_error(
    tmp_path: Path,
    payload: dict[str, object],
    expected: str,
) -> None:
    _write(tmp_path, "README.md", "# 项目\n")
    manifest = tmp_path / "docs" / "governance.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_repository(tmp_path, manifest)

    assert report.errors
    assert expected in report.errors[0]
    assert "治理清单" in report.errors[0]


def test_cli_exit_codes_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "README.md", "# 项目\n")
    manifest = _write_manifest(
        tmp_path,
        [{"pattern": "README.md", "status": "current", "enforce_current": True}],
    )

    assert main(["--root", str(tmp_path), "--manifest", str(manifest)]) == 0
    success = capsys.readouterr()
    assert "文档治理检查通过" in success.out

    _write(tmp_path, "docs/orphan.md", "# 无归属\n")
    assert main(["--root", str(tmp_path), "--manifest", str(manifest)]) == 1
    failure = capsys.readouterr()
    assert "文档治理检查失败" in failure.err
    assert "docs/orphan.md" in failure.err


def test_real_manifest_classifies_every_repository_document() -> None:
    root = Path(__file__).resolve().parents[2]

    report = validate_repository(root)

    classification_errors = [
        error
        for error in report.errors
        if "未分类" in error or "多个分类规则" in error
    ]
    assert classification_errors == []
