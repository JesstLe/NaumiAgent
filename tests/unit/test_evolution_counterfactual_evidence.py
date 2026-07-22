from __future__ import annotations

import difflib
import hashlib

import naumi_agent.evolution.counterfactual_evidence as counterfactual_module
import naumi_agent.evolution.mutation_receipts as mutation_receipt_module
from naumi_agent.evolution.counterfactual_evidence import (
    CounterfactualFindingCode,
)
from naumi_agent.evolution.mutation_receipts import MutationReceiptFile


def _receipt(path: str, before: bytes | None, after: bytes) -> MutationReceiptFile:
    before_lines = tuple(before.decode("utf-8").splitlines()) if before is not None else ()
    after_lines = tuple(after.decode("utf-8").splitlines())
    changes = counterfactual_module._changed_lines(before_lines, after_lines)  # noqa: SLF001
    unified = tuple(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    ))
    payload = {
        "path": path,
        "operation": "modify" if before is not None else "create",
        "before_sha256": (
            hashlib.sha256(before).hexdigest() if before is not None else None
        ),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "unified_diff_sha256": counterfactual_module._sha256_payload(unified),  # noqa: SLF001
        "added_lines": sum(item.direction == "added" for item in changes),
        "deleted_lines": sum(item.direction == "deleted" for item in changes),
        "api_change": "unchanged" if before is not None else "additive",
    }
    return MutationReceiptFile.model_validate({
        **payload,
        "fact_sha256": mutation_receipt_module._sha256_payload(payload),  # noqa: SLF001
    })


def test_counterfactual_scan_detects_direct_alternative_explanations() -> None:
    before = b"""def test_quality():
    assert score >= 0.8
    assert result.ok
"""
    after = b"""@pytest.mark.skip(reason=\"flaky\")
def test_quality():
    assert score >= 0.5
    fake = MagicMock()
    expected_output = ground_truth
"""
    receipt = _receipt("tests/test_quality.py", before, after)

    evidence, findings = counterfactual_module._scan_file(  # noqa: SLF001
        receipt,
        before=before,
        after=after,
        required_metrics=("score",),
    )

    codes = {item.code for item in findings}
    assert CounterfactualFindingCode.TEST_DELETION in codes
    assert CounterfactualFindingCode.METRIC_MUTATION in codes
    assert CounterfactualFindingCode.THRESHOLD_RELAXATION in codes
    assert CounterfactualFindingCode.SKIP_ADDED in codes
    assert CounterfactualFindingCode.MOCK_ADDED in codes
    assert CounterfactualFindingCode.EVALUATION_LEAKAGE in codes
    assert evidence.smaller_scope_plausible is True
    assert evidence.after_sha256 == receipt.after_sha256
    assert evidence.unified_diff_sha256 == receipt.unified_diff_sha256
    serialized = evidence.model_dump_json() + "".join(
        item.model_dump_json() for item in findings
    )
    assert "ground_truth" not in serialized
    assert "MagicMock" not in serialized
    assert "pytest.mark.skip" not in serialized


def test_counterfactual_scan_marks_whitespace_only_file_as_reducible() -> None:
    before = b"value = 1\n"
    after = b"value = 1   \n"
    receipt = _receipt("src/example.py", before, after)

    evidence, findings = counterfactual_module._scan_file(  # noqa: SLF001
        receipt,
        before=before,
        after=after,
        required_metrics=("quality",),
    )

    assert evidence.semantic_added_lines == 0
    assert evidence.semantic_deleted_lines == 0
    assert evidence.smaller_scope_plausible is True
    assert tuple(item.code for item in findings) == (
        CounterfactualFindingCode.NON_SEMANTIC_SCOPE,
    )


def test_counterfactual_scan_rejects_candidate_digest_drift() -> None:
    before = b"value = 1\n"
    after = b"value = 2\n"
    receipt = _receipt("src/example.py", before, after)

    try:
        counterfactual_module._scan_file(  # noqa: SLF001
            receipt,
            before=before,
            after=b"value = 3\n",
            required_metrics=("quality",),
        )
    except counterfactual_module.EvolutionCounterfactualEvidenceError as exc:
        assert exc.code == "counterfactual_file_digest_mismatch"
    else:
        raise AssertionError("candidate digest drift must fail closed")
