"""Deterministic semantic pointer architecture analysis helpers."""

from __future__ import annotations

import re
from pathlib import Path

PRECISION_PATTERNS = [
    (r"(?:Decimal|decimal\.Decimal|float|double|np\.float\d*)", "浮点精确类型"),
    (r"(?:money|currency|price|amount|balance|fee)", "金融金额"),
    (r"(?:PE|EPS|ROE|ROI|NAV| sharpe|alpha|beta)\b", "金融指标"),
    (r"(?:dosage|blood_pressure|heart_rate|diagnosis)", "医疗数据"),
    (r"(?:coordinate|altitude|velocity|trajectory|orbit)", "航天/物理数据"),
    (r"(?:hash|checksum|signature|token|secret|key)\b", "安全哈希"),
    (r"(?:id|uuid|guid|serial)\s*[:=]\s*[\"']\w+", "唯一标识符"),
]

POINTER_SOURCES = [
    (r"(?:api|fetch|get|query|request)\s*\([^)]*stock", "股票 API"),
    (r"(?:api|fetch|get|query|request)\s*\([^)]*price", "价格 API"),
    (r"(?:\.execute\(|cursor\.|session\.query)", "数据库查询"),
    (r"(?:redis\.get|cache\.get|memcached)", "缓存读取"),
    (r"(?:pd\.read_|read_csv|read_json|read_parquet)", "数据文件读取"),
    (r"(?:requests\.(get|post)|httpx\.client)", "HTTP 数据源"),
]

BOUNDARY_PATTERNS = [
    (r"return\s+str\(.*(?:price|amount|balance)", "数值转字符串返回"),
    (r"f[\"'].*{(?:price|amount|pe|eps).*}[\"']", "f-string 插入金融数据"),
    (r"(?:format|round)\s*\(.*(?:price|amount|rate)", "金融数据格式化"),
    (r"json\.dumps\s*\([^)]*(?:result|data|response)", "JSON 序列化 AI 输出"),
    (r"response\s*[:=]\s*(?:await\s+)?(?:llm|model|chat|complete)", "LLM 原始输出"),
]


def scan_pointer(files: list[Path], source_text: str, target: str) -> str:
    """Detect hallucination risks and pointer-friendly data boundaries."""
    del files, target

    findings: list[str] = []

    precision_hits: list[tuple[str, int]] = []
    for pattern, label in PRECISION_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            precision_hits.append((label, count))

    if precision_hits:
        findings.append("- 精密数据类型（幻觉高风险）:")
        for label, count in precision_hits:
            findings.append(f"  - {label}: {count} 处引用")
    else:
        findings.append("- 精密数据类型: 未检测到")

    pointer_sources: list[tuple[str, int]] = []
    for pattern, label in POINTER_SOURCES:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            pointer_sources.append((label, count))

    if pointer_sources:
        findings.append("- 可指针化的数据源（建议物理态隔离）:")
        for label, count in pointer_sources:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 外部数据源: 未检测到")

    boundary_hits: list[tuple[str, int]] = []
    for pattern, label in BOUNDARY_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            boundary_hits.append((label, count))

    if boundary_hits:
        findings.append(
            "- ⚠️ 推理态/物理态边界风险点: "
            f"{sum(count for _, count in boundary_hits)} 处"
        )
        for label, count in boundary_hits:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 边界风险: 未检测到明显风险")

    magic_numbers = re.findall(
        r"(?<!self\.)(?:price|rate|ratio|threshold)\s*[=:]\s*[\d.]+",
        source_text,
        re.IGNORECASE,
    )
    if magic_numbers:
        findings.append(f"- ⚠️ 硬编码数值: {len(magic_numbers)} 处（应改为指针引用外部数据源）")
        for match in magic_numbers[:5]:
            findings.append(f"  - `{match.strip()}`")

    has_dao = bool(re.findall(r"(?:Repository|DAO|Mapper|Gateway)", source_text))
    has_service = bool(re.findall(r"(?:Service|Manager|Handler)", source_text))
    has_controller = bool(
        re.findall(r"(?:Controller|Router|Endpoint|View)", source_text)
    )
    layers = []
    if has_dao:
        layers.append("数据层(DAO)")
    if has_service:
        layers.append("服务层(Service)")
    if has_controller:
        layers.append("控制层(Controller)")
    if layers:
        findings.append(f"- 已有分层: {' → '.join(layers)}")
    else:
        findings.append("- ⚠️ 无明显分层架构（需要 SPA 重构）")

    risk_score = 0
    risk_score += sum(count for _, count in precision_hits) * 5
    risk_score += sum(count for _, count in boundary_hits) * 8
    risk_score += len(magic_numbers) * 10
    if not layers:
        risk_score += 20

    level = (
        "CRITICAL"
        if risk_score > 100
        else "HIGH"
        if risk_score > 50
        else "MEDIUM"
        if risk_score > 20
        else "LOW"
    )
    findings.append(f"\n- 幻觉风险评分: {risk_score} ({level})")
    if level in ("HIGH", "CRITICAL"):
        findings.append(
            "  → 强烈建议：将精密数据计算剥离为独立模块，"
            "AI 仅通过指针(API调用)获取结果"
        )

    return "\n".join(findings)


def build_pointer_report(scan_evidence: str, files: list[Path], context: str = "") -> str:
    """Build a deterministic semantic pointer architecture report."""
    pointers = infer_pointer_table(files)
    lines = [
        "## SPA 确定性指针架构",
        f"- 扫描文件数：{len(files)}",
        f"- 业务上下文：{context or '未提供'}",
        "",
        "## 风险扫描",
        scan_evidence,
        "",
        "## Reasoning Space",
        "- AI 负责：策略解释、流程编排、自然语言交互、选择 pointer。",
        "- AI 禁止：直接生成金额、精度敏感指标、token/hash、医疗/物理测量值。",
        "- AI 输出必须携带 pointer id，而不是伪造物理态数据。",
        "",
        "## Physical Space",
        "- 精确计算、DB/API 查询、Decimal/类型校验必须由普通代码完成。",
        "- dereference 模块必须返回结构化结果：value、source、timestamp、validation。",
        "- 失败必须显式返回 null/error，不允许由 AI 猜测补全。",
        "",
        "## Pointer Table",
        "| Pointer | Dereference Module | Input | Output | Risk Level |",
        "|---------|-------------------|-------|--------|------------|",
    ]
    lines.extend(pointers)
    lines.extend(
        [
            "",
            "## Migration Plan",
            "1. 先替换风险评分最高的边界，让 AI 输出 pointer token。",
            "2. 为每个 dereference 函数补类型校验和空值/错误返回。",
            "3. 用 Decimal/结构化对象替代 float/string 拼接。",
            "4. 为 pointer 结果记录来源、时间戳和校验状态。",
            "5. 用 targeted tests 覆盖：正常数据、缺失数据、异常数据、过期数据。",
        ]
    )
    return "\n".join(lines)


def infer_pointer_table(files: list[Path]) -> list[str]:
    """Infer concrete pointer table rows from source file signals."""
    rows: list[str] = []
    for file in files:
        try:
            content = file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lower = content.lower()
        if any(token in lower for token in ("price", "amount", "balance", "currency")):
            rows.append(
                "| `finance.price_ref` | `finance_gateway.get_decimal()` | "
                "`symbol/account_id` | `Decimal + source` | HIGH |"
            )
        if any(token in lower for token in ("token", "secret", "signature", "hash")):
            rows.append(
                "| `security.secret_ref` | `secret_store.get()` | "
                "`secret_id` | `opaque bytes / redacted` | CRITICAL |"
            )
        if any(token in lower for token in ("diagnosis", "dosage", "blood_pressure")):
            rows.append(
                "| `medical.measurement_ref` | `clinical_store.get_measurement()` | "
                "`patient_id + metric` | `validated measurement` | CRITICAL |"
            )
        if re.search(r"(?:\.execute\(|session\.query|cursor\.)", content):
            rows.append(
                "| `db.query_ref` | `repository.fetch()` | "
                "`query object` | `typed record set` | HIGH |"
            )
        if re.search(r"(?:requests\.(get|post)|httpx\.client)", content, re.IGNORECASE):
            rows.append(
                "| `api.response_ref` | `api_client.fetch_validated()` | "
                "`endpoint + params` | `schema-validated payload` | MEDIUM |"
            )
    if not rows:
        rows.append(
            "| `domain.value_ref` | `typed_repository.get()` | "
            "`entity_id` | `validated domain object` | MEDIUM |"
        )
    return list(dict.fromkeys(rows))
