"""Deterministic quotation, freshness, quantity and dependency checks for research.

The service supplies already-authorized exact source versions. Matching quoted
bytes is not a semantic entailment proof, and none of these checks confers one.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, localcontext

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_models import (
    MAX_DECIMAL,
    ResearchClaim,
    ResearchConclusion,
    decimal_value,
    validate_payload,
)

_SHA = re.compile(r"^[a-f0-9]{64}$")
_NUMBERS = re.compile(
    r"(?<![0-9.])[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d{1,3})?(?![0-9.])"
)
_AVAILABLE = frozenset({"available", "captured", "provided", "derived", "retrieved", "ready"})
_CURRENCIES = {
    "USD": (r"\bUSD\b", "美元", "美金", r"US\$"),
    "EUR": (r"\bEUR\b", "欧元", "€"),
    "CNY": (r"\bCNY\b", r"\bRMB\b", "人民币", "￥"),
    "JPY": (r"\bJPY\b", "日元"),
    "GBP": (r"\bGBP\b", "英镑", "£"),
    "HKD": (r"\bHKD\b", "港币", "港元", r"HK\$"),
    "AUD": (r"\bAUD\b", "澳元", r"AU\$"),
    "CAD": (r"\bCAD\b", "加元", r"CA\$"),
}
_UNIT_ALIASES = {
    "%": "percent",
    "％": "percent",
    "percent": "percent",
    "percentage": "percent",
    "百分比": "percent",
    "百分数": "percent",
    "ratio": "ratio",
    "比例": "ratio",
    "人": "person",
    "person": "person",
    "persons": "person",
    "people": "person",
    "count": "count",
    "次": "count",
    "个": "count",
    "件": "count",
    "usd": "currency",
    "eur": "currency",
    "cny": "currency",
    "jpy": "currency",
    "gbp": "currency",
    "hkd": "currency",
    "aud": "currency",
    "cad": "currency",
    "currency": "currency",
    "元": "currency",
    "美元": "currency",
    "欧元": "currency",
    "人民币": "currency",
    "日元": "currency",
    "港元": "currency",
    "英镑": "currency",
    "$": "currency",
    "€": "currency",
    "¥": "currency",
    "￥": "currency",
    "£": "currency",
}


def _number(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"-0", "+0", ""} else result


def _unit(value):
    key = value.strip().casefold()
    return _UNIT_ALIASES.get(key, key)


def _instant(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _source_for(sources, source_id, revision):
    """Resolve a requested historical version without falling back to current text."""
    value = sources.get(source_id)
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("revisions"), dict):
        historical = value["revisions"].get(str(revision), value["revisions"].get(revision))
        if not isinstance(historical, dict):
            return None
        value = {
            **historical,
            "latest_revision": max(
                candidate
                for candidate in (value.get("latest_revision"), value.get("revision"))
                if type(candidate) is int
            )
            if any(
                type(candidate) is int
                for candidate in (value.get("latest_revision"), value.get("revision"))
            )
            else None,
            "current_digest": value.get("current_digest", value.get("content_sha256")),
            "current_status": value.get("current_status", value.get("status")),
            "upstream_unverified": bool(
                historical.get("upstream_unverified") or value.get("upstream_unverified")
            ),
        }
    actual_revision = value.get("revision", value.get("source_revision"))
    if type(actual_revision) is not int or actual_revision != revision:
        return None
    return value


def _source_hash(source):
    artifact = source.get("artifact_ref")
    return (
        source.get("content_sha256")
        or source.get("body_sha256")
        or (artifact.get("sha256") if isinstance(artifact, dict) else None)
        or source.get("sha256")
        or source.get("digest")
    )


def _upstream_for(upstream, reference):
    if not isinstance(upstream, dict):
        return None
    key = (reference["notebook_id"], reference["revision"], reference["claim_id"])
    value = upstream.get(key)
    if value is None:
        value = upstream.get(f"{key[0]}:{key[1]}:{key[2]}")
    return value if isinstance(value, dict) else None


def _quote_start(body, quote, occurrence):
    start = 0
    for _ in range(occurrence + 1):
        position = body.find(quote, start)
        if position < 0:
            return None
        start = position + len(quote)
    return position


def _literal_numbers(quote):
    values = set()
    for match in _NUMBERS.finditer(quote):
        try:
            values.add(decimal_value(match.group().replace(",", "")))
        except ValueError:
            continue
    return values


def _quantity_evidence(quotes, value):
    """Inspect only units immediately adjacent to the matching numeric literal."""
    currencies, percent, money_symbol = set(), False, False
    for quote in quotes:
        for match in _NUMBERS.finditer(quote):
            try:
                if decimal_value(match.group().replace(",", "")) != value:
                    continue
            except ValueError:
                continue
            before, after = quote[max(0, match.start() - 16) : match.start()], quote[match.end() :]
            percent = percent or bool(
                re.match(r"\s*(?:%|％|percent\b|percentage\b|百分比|百分点)", after, re.IGNORECASE)
                or re.search(r"百分之\s*$", before)
            )
            money_symbol = money_symbol or bool(re.search(r"[$€¥￥£]\s*$", before))
            for code, patterns in _CURRENCIES.items():
                if any(
                    re.search(r"(?:" + pattern + r")\s*$", before, re.IGNORECASE)
                    or re.match(r"\s*(?:" + pattern + r")(?![A-Za-z])", after, re.IGNORECASE)
                    for pattern in patterns
                ):
                    currencies.add(code)
    return currencies, percent, money_symbol


def _cycles(claims):
    """Find cyclic calculation dependencies without recursively evaluating expressions."""
    graph = {
        claim["id"]: claim["calculation"]["inputs"] if claim.get("calculation") else []
        for claim in claims
    }
    state, stack, cyclic = {}, [], set()

    def visit(identity):
        if state.get(identity) == 1:
            cyclic.update(stack[stack.index(identity) :])
            return
        if state.get(identity) == 2 or identity not in graph:
            return
        state[identity] = 1
        stack.append(identity)
        for other in graph[identity]:
            visit(other)
        stack.pop()
        state[identity] = 2

    for identity in graph:
        visit(identity)
    return cyclic


def review_notebook(notebook, sources, upstream, now):
    """Check exact authorized evidence, retaining all uncertainty and original kinds.

    ``sources`` is keyed by source ID. Each entry has the historical ``revision``,
    exact UTF-8 ``body``, ``status``, ``expires_at`` and a body hash in
    ``content_sha256``/``body_sha256``/``artifact_ref.sha256``. Current metadata may
    be supplied as ``latest_revision``, ``current_digest`` and ``current_status``.
    Multiple pinned revisions can be supplied in ``entry['revisions']``.

    ``upstream`` maps ``(notebook_id, revision, claim_id)`` (or its colon-joined
    string) to service-authorized availability and verification facts. Missing
    proof is never treated as an independently verified source.
    """
    instant = _instant(now)
    if instant is None:
        raise PersonalAgentError("invalid_input", "复核时刻需要明确时区")
    if not isinstance(notebook, dict) or not isinstance(sources, dict):
        raise PersonalAgentError("invalid_input", "研究记录或来源格式不正确")
    raw_claims = notebook.get("claims", [])
    if not isinstance(raw_claims, list) or len(raw_claims) > 64:
        raise PersonalAgentError("invalid_input", "研究结论数量或格式不正确")
    claims = [validate_payload(ResearchClaim, claim) for claim in raw_claims]
    if len(claims) != len({claim["id"] for claim in claims}):
        raise PersonalAgentError("invalid_input", "研究结论编号重复")
    conclusion = validate_payload(ResearchConclusion, notebook.get("conclusion", {}))
    by_id = {claim["id"]: claim for claim in claims}
    findings, anchors, calculations, quantities = [], [], [], []
    keys, source_cache, quote_by_claim = set(), {}, {}
    upstream_unverified = set()

    def finding(code, claim_id, message, severity="error", source_id=None):
        key = (code, claim_id, source_id)
        if key in keys:
            return
        keys.add(key)
        value = {"code": code, "claim_id": claim_id, "message": message, "severity": severity}
        if source_id is not None:
            value["source_id"] = source_id
        findings.append(value)

    def source_check(source_id, revision):
        cache_key = (source_id, revision)
        if cache_key in source_cache:
            return source_cache[cache_key]
        source = _source_for(sources, source_id, revision)
        issues = []
        body = None
        if source is None:
            issues.append(
                ("source_version_unavailable", "原引用版本不可用，请重新核对来源", "error")
            )
        else:
            status = source.get("current_status", source.get("status"))
            if status not in _AVAILABLE or source.get("available") is False:
                issues.append(("source_unavailable", "原来源已失效或访问已撤销", "error"))
            elif not isinstance(source.get("body"), str):
                issues.append(("source_body_unavailable", "原来源正文尚不可读", "error"))
            else:
                candidate = source["body"]
                try:
                    raw = candidate.encode("utf-8")
                except UnicodeError:
                    raw = None
                expected_hash = _source_hash(source)
                if raw is None or len(raw) > 1024 * 1024:
                    issues.append(("source_body_invalid", "原来源正文格式或容量不正确", "error"))
                elif not isinstance(expected_hash, str) or _SHA.fullmatch(expected_hash) is None:
                    issues.append(("source_digest_unavailable", "原来源缺少确切正文摘要", "error"))
                elif hashlib.sha256(raw).hexdigest() != expected_hash:
                    issues.append(("source_integrity_mismatch", "原来源正文与摘要不一致", "error"))
                else:
                    body = candidate
                current_digest = source.get("current_digest")
                if current_digest is not None and current_digest != expected_hash:
                    issues.append(("source_changed", "来源当前正文已变化，原引用需要修订", "error"))
                latest_revision = source.get("latest_revision", revision)
                if latest_revision != revision:
                    issues.append(
                        ("historical_source", "引用不是来源当前版本，请保留历史状态", "warning")
                    )
                expiry = _instant(source.get("expires_at"))
                if expiry is None:
                    issues.append(("source_expiry_unknown", "来源缺少有效时效边界", "warning"))
                elif expiry <= instant:
                    issues.append(("source_expired", "来源已超过时效边界，需要重新核对", "warning"))
                observed_raw = source.get("captured_at", source.get("observed_at"))
                observed = _instant(observed_raw) if observed_raw is not None else None
                if observed_raw is not None and (observed is None or observed > instant):
                    issues.append(
                        ("source_observation_invalid", "来源抓取时刻无效或晚于复核时刻", "error")
                    )
                if source.get("upstream_unverified") or source.get("origin") == "derived":
                    issues.append(
                        (
                            "upstream_unverified",
                            "来源包含上游未核验内容，不能升级为独立核实",
                            "warning",
                        )
                    )
        result = (body, issues)
        source_cache[cache_key] = result
        return result

    for claim in claims:
        identity = claim["id"]
        quote_by_claim[identity] = []
        if claim["kind"] == "unknown":
            finding("unknown_claim", identity, "该结论仍为未知，复核不会将其升级为事实", "warning")
        elif claim["kind"] == "fact" and not claim["citations"]:
            finding("missing_citation", identity, "事实结论需要可定位的原文引用")
        elif claim["kind"] == "estimate" and not claim["assumptions"]:
            finding("missing_assumptions", identity, "估算需要明确方法或前提")
        for citation in claim["citations"]:
            source_id, revision = citation["source_id"], citation["source_revision"]
            body, issues = source_check(source_id, revision)
            for code, message, severity in issues:
                finding(code, identity, message, severity, source_id)
                if code == "upstream_unverified":
                    upstream_unverified.add(identity)
            if body is None:
                continue
            quote = citation["quote"]
            start = _quote_start(body, quote, citation["occurrence"])
            if start is None:
                finding(
                    "quote_not_found",
                    identity,
                    "引用正文或指定出现位置不在原版本中",
                    source_id=source_id,
                )
                continue
            end = start + len(quote)
            anchors.append(
                {
                    "claim_id": identity,
                    "source_id": source_id,
                    "source_revision": revision,
                    "quote": quote,
                    "start": start,
                    "end": end,
                    "byte_start": len(body[:start].encode("utf-8")),
                    "byte_end": len(body[:end].encode("utf-8")),
                    "sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                }
            )
            quote_by_claim[identity].append(quote)
        for reference in claim["upstream_claims"]:
            original = _upstream_for(upstream, reference)
            if original is None or original.get("available") is not True:
                finding("upstream_unavailable", identity, "原上游结论或其访问证明不可用", "warning")
                upstream_unverified.add(identity)
                continue
            if (
                original.get("upstream_unverified")
                or original.get("kind") in {"estimate", "unknown"}
                or original.get("semantic_verification") != "verified"
                or original.get("status")
                in {"issues_found", "needs_sources", "unavailable", "stale"}
            ):
                finding(
                    "upstream_unverified",
                    identity,
                    "上游未独立核实，转述不能清除其未核验标签",
                    "warning",
                )
                upstream_unverified.add(identity)
        quantity = claim.get("quantity")
        if quantity is None:
            continue
        value = decimal_value(quantity["value"])
        quantities.append(
            {
                "claim_id": identity,
                **quantity,
                "normalized_value": _number(value),
                "normalized_unit": _unit(quantity["unit"]),
                "kind": claim["kind"],
            }
        )
        quotes = quote_by_claim[identity]
        if claim.get("calculation"):
            # Derived quantities are checked against their exact input graph below;
            # a coincident source numeral is not the derived output's unit proof.
            continue
        if claim["kind"] == "fact" and not claim.get("calculation") and quotes:
            if not any(value in _literal_numbers(quote) for quote in quotes):
                finding("quantity_not_in_quote", identity, "声明数值未出现在该结论的确切引用中")
        quote_currencies, quote_percent, quote_money = _quantity_evidence(quotes, value)
        if (
            quantity["currency"] is not None
            and quote_currencies
            and (quantity["currency"] not in quote_currencies)
        ):
            finding("currency_mismatch", identity, "声明币种与引用中的明确币种不一致")
        if quantity["currency"] is None and quote_currencies:
            finding(
                "currency_unspecified", identity, "引用含明确币种，声明数量尚未指定币种", "warning"
            )
        if quote_money and quantity["currency"] is None:
            finding(
                "currency_unspecified", identity, "引用含货币符号，声明数量尚未指定币种", "warning"
            )
        if quote_percent:
            if _unit(quantity["unit"]) != "percent" and not claim.get("calculation"):
                finding("unit_mismatch", identity, "引用使用百分数，声明单位未与百分数对齐")

    cyclic = _cycles(claims)
    for identity in sorted(cyclic):
        finding("calculation_cycle", identity, "计算输入形成循环，不能形成独立复算依据")

    def claim_has_problem(identity):
        return any(item["claim_id"] == identity for item in findings)

    completed = {}

    def calculate(identity):
        if identity in completed:
            return completed[identity]
        claim = by_id[identity]
        calculation = claim.get("calculation")
        if calculation is None or identity in cyclic:
            completed[identity] = None
            return None
        inputs = calculation["inputs"]
        if any(other not in by_id for other in inputs):
            finding("calculation_input_missing", identity, "计算引用了不存在的结论编号")
            completed[identity] = None
            return None
        input_claims = [by_id[other] for other in inputs]
        for other in inputs:
            calculate(other)
        if any(
            value.get("quantity") is None or value["kind"] == "unknown" for value in input_claims
        ):
            finding("calculation_input_unknown", identity, "计算输入缺少明确数量或仍为未知")
            completed[identity] = None
            return None
        input_quantities = [value["quantity"] for value in input_claims]
        units = {_unit(value["unit"]) for value in input_quantities}
        currencies = {value["currency"] for value in input_quantities}
        periods = {value["period"] for value in input_quantities}
        incompatible = False
        for values, code, message in (
            (units, "calculation_unit_mismatch", "计算输入的单位不兼容，需先明确换算"),
            (
                currencies,
                "calculation_currency_mismatch",
                "计算输入的币种不一致，需先明确汇率和时点",
            ),
            (periods, "calculation_period_mismatch", "计算输入的统计时段不一致"),
        ):
            if len(values) != 1:
                finding(code, identity, message)
                incompatible = True
        if incompatible:
            completed[identity] = None
            return None
        values = [decimal_value(value["value"]) for value in input_quantities]
        operation = calculation["operation"]
        try:
            with localcontext() as context:
                context.prec = 80
                if operation in {"ratio", "percent"}:
                    if values[1] == 0:
                        finding("division_by_zero", identity, "比例计算的分母为零")
                        completed[identity] = None
                        return None
                    result = values[0] / values[1] * (100 if operation == "percent" else 1)
                elif operation == "sum":
                    result = sum(values, Decimal(0))
                else:
                    result = values[0] - sum(values[1:], Decimal(0))
                if not result.is_finite() or result.copy_abs() > MAX_DECIMAL:
                    finding("calculation_out_of_range", identity, "复算结果超过允许的数值范围")
                    completed[identity] = None
                    return None
                expected = decimal_value(calculation["expected"])
                tolerance = decimal_value(calculation["tolerance"], nonnegative=True)
                difference = (result - expected).copy_abs()
                matches = difference <= tolerance
                if not matches:
                    finding(
                        "calculation_mismatch",
                        identity,
                        f"复算结果为 {_number(result)}，与声明结果 {_number(expected)} 不一致",
                    )
                elif difference:
                    finding(
                        "calculation_rounded",
                        identity,
                        "声明结果与精确复算值存在差异，已按明确容差保留近似标记",
                        "warning",
                    )
                if tolerance > Decimal("0.01"):
                    finding(
                        "calculation_tolerance_large",
                        identity,
                        "计算容差较大，不能作为严格相等证明",
                        "warning",
                    )
                quantity = claim.get("quantity")
                if quantity is not None:
                    if (decimal_value(quantity["value"]) - result).copy_abs() > tolerance:
                        finding(
                            "calculated_quantity_mismatch", identity, "结论数量与实际复算结果不一致"
                        )
                    output_unit = (
                        "percent"
                        if operation == "percent"
                        else "ratio"
                        if operation == "ratio"
                        else next(iter(units))
                    )
                    output_currency = (
                        None if operation in {"ratio", "percent"} else next(iter(currencies))
                    )
                    if _unit(quantity["unit"]) != output_unit:
                        finding(
                            "calculation_output_unit_mismatch",
                            identity,
                            "复算结果与结论的输出单位不一致",
                        )
                    if quantity["currency"] != output_currency:
                        finding(
                            "calculation_output_currency_mismatch",
                            identity,
                            "复算结果与结论的输出币种不一致",
                        )
                    if quantity["period"] != next(iter(periods)):
                        finding(
                            "calculation_output_period_mismatch",
                            identity,
                            "复算结果与结论的统计时段不一致",
                        )
                anchored = all(
                    not claim_has_problem(other)
                    and quote_by_claim[other]
                    and by_id[other]["kind"] == "fact"
                    for other in inputs
                )
                if not anchored:
                    finding(
                        "calculation_inputs_unverified",
                        identity,
                        "计算输入仍含未核验、估算或有问题的数量，结果保留原不确定性",
                        "warning",
                    )
                    upstream_unverified.add(identity)
                row = {
                    "claim_id": identity,
                    "operation": operation,
                    "inputs": inputs,
                    "computed": _number(result),
                    "expected": calculation["expected"],
                    "tolerance": calculation["tolerance"],
                    "matches": matches,
                    "exact_matches": difference == 0,
                    "inputs_anchored": bool(anchored),
                    "semantic_verification": "not_performed",
                }
                calculations.append(row)
                completed[identity] = row
                return row
        except DecimalException:
            finding("calculation_invalid", identity, "数量运算未形成有效复算结果")
            completed[identity] = None
            return None

    for claim in claims:
        calculate(claim["id"])

    for index, left in enumerate(claims):
        if not left["subject"].strip() or left.get("quantity") is None or left["kind"] == "unknown":
            continue
        for right in claims[index + 1 :]:
            if (
                right["subject"].strip().casefold() != left["subject"].strip().casefold()
                or right.get("quantity") is None
                or right["kind"] == "unknown"
                or right["quantity"]["period"] != left["quantity"]["period"]
            ):
                continue
            first, second = left["quantity"], right["quantity"]
            if _unit(first["unit"]) != _unit(second["unit"]):
                for item in (left, right):
                    finding(
                        "subject_unit_unaligned", item["id"], "同一主体的数量单位未对齐", "warning"
                    )
            elif first["currency"] != second["currency"]:
                for item in (left, right):
                    finding(
                        "subject_currency_unaligned",
                        item["id"],
                        "同一主体的数量币种未对齐",
                        "warning",
                    )
            elif decimal_value(first["value"]) != decimal_value(second["value"]):
                estimated = left["kind"] == "estimate" or right["kind"] == "estimate"
                for item in (left, right):
                    finding(
                        "estimate_difference" if estimated else "quantity_conflict",
                        item["id"],
                        "同一主体和时段存在不同估算，请保留各自前提"
                        if estimated
                        else "同一主体、单位和时段出现不一致的事实数量",
                        "warning" if estimated else "error",
                    )

    # Conflicts are detected after arithmetic. Reconcile dependencies again so a
    # later input finding cannot leave a downstream calculation falsely anchored.
    # Rows are appended dependencies-first by calculate(), including nested graphs.
    for row in calculations:
        if any(
            claim_has_problem(other)
            or not quote_by_claim[other]
            or by_id[other]["kind"] != "fact"
            for other in row["inputs"]
        ):
            row["inputs_anchored"] = False
            finding(
                "calculation_inputs_unverified",
                row["claim_id"],
                "计算输入仍含未核验、估算或有问题的数量，结果保留原不确定性",
                "warning",
            )
            upstream_unverified.add(row["claim_id"])

    if conclusion["stance"] != "undecided":
        if not conclusion["premises"]:
            finding("missing_conclusion_premises", None, "明确判断需要列出成立前提", "warning")
        if not conclusion["invalidators"]:
            finding("missing_invalidators", None, "明确判断需要列出失效条件或反证", "warning")
    if not claims:
        finding("no_claims", None, "研究尚无可复核结论", "warning")
    claim_states = []
    for claim in claims:
        identity = claim["id"]
        problems = [item for item in findings if item["claim_id"] == identity]
        state = (
            "unknown"
            if claim["kind"] == "unknown"
            else "issue"
            if any(item["severity"] == "error" for item in problems)
            else "unverified"
            if problems
            else "estimated"
            if claim["kind"] == "estimate"
            else "anchored"
            if quote_by_claim[identity]
            else "unverified"
        )
        claim_states.append(
            {
                "claim_id": identity,
                "kind": claim["kind"],
                "state": state,
                "independently_verified": False,
                "upstream_unverified": identity in upstream_unverified,
            }
        )
    status = (
        "issues_found"
        if any(item["severity"] == "error" for item in findings)
        else ("needs_sources" if findings else "mechanically_checked")
    )
    return {
        "status": status,
        "semantic_verification": "not_performed",
        "findings": findings,
        "anchors": anchors,
        "quantities": quantities,
        "calculations": calculations,
        "claim_states": claim_states,
        "checked_at": instant.isoformat(timespec="microseconds"),
    }
