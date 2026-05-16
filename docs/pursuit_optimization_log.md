# /pursue 优化记录

> 测试用例：为 config.yaml 添加一行 temperature 注释（说明 kimi-k2.6 限制）
> 基线模型：openai/kimi-for-coding (kimi-k2.6, thinking enabled)

## 优化历史

### v1 — 基线（重写全文件）

| 指标 | 值 |
|------|------|
| 轮次 | 4 |
| Tokens | 38,922 |
| 耗时 | ~9 min |
| 成本 | $0.43 |
| 文件质量 | 缩进错乱 |

**问题：** `_edit_small_file` 让 LLM 重写整个文件，对 YAML 等格式敏感文件易破坏缩进。

**commit:** `719fa3c` fix: kimi-k2.6 compatibility and pursuit loop robustness

---

### v2 — search/replace 替代全文件重写

| 指标 | 值 | 变化 |
|------|------|------|
| 轮次 | 4-6 | 持平/退化 |
| Tokens | 38k-72k | 持平/退化 |
| 文件质量 | 缩进完好 | 改善 |

**改动：** 用 `[SEARCH]/[REPLACE]/[END]` 块替代全文件重写，保留原始文件结构。

**关键发现：** 文件质量大幅改善，但 LLM（K2.6）不总是遵循格式，导致解析失败、循环反复。

**commit:** `127aa33` refactor: pursuit file edit uses search/replace instead of full rewrite

---

### v3 — 路径匹配修复 + few-shot 示例

| 指标 | 值 | 变化 |
|------|------|------|
| 轮次 | 2 | -50% |
| Tokens | 17,465 | -55% |
| 耗时 | ~5.8 min | -36% |
| 成本 | $0.18 | -58% |

**改动：**
- 修复路径正则 `r'([\w/.]+\.py)'` → `r'([\w/.]+\.\w+)'`（原来只匹配 .py，匹配到 k2.6/1.0）
- 添加 `_extract_target_path` 严格扩展名匹配
- 给 search/replace prompt 加 few-shot 示例
- Planner prompt 要求 description 必须包含文件路径

**commit:** `bf7cba7` perf: pursuit file_edit 2-round convergence with robust path matching

---

### v4 — thinking 参数 + 路径注入

| 指标 | 值 | 变化 |
|------|------|------|
| 轮次 | 2 | 持平 |
| Tokens | 25,159 | +44%（thinking 开销） |
| 耗时 | ~8 min | +38% |

**改动：**
- kimi-k2.6 thinking 参数支持（`extra_body={"thinking": {"type": "enabled"}}`）
- LLM action description 缺文件路径时，从 goal spec 自动注入

**权衡：** tokens 增加是因为 thinking tokens 消耗预算，但 search/replace 格式遵循度更高。

**commit:** `4cf8717` feat: kimi-k2.6 thinking parameter support  
**commit:** `69a1940` fix: inject file path from goal when action description lacks it

---

### v5 — 收敛度程序化底线（当前）

| 指标 | 值 | 变化 |
|------|------|------|
| 轮次 | 2 | 持平 |
| Tokens | 13,700 | **-45% vs v4** |
| 耗时 | ~4.7 min | -41% |
| 成本 | $0.15 | -17% |

**改动：**
- 收敛度程序化底线：completed actions + git diff 有变更 → convergence ≥ 0.5
- git diff 改用 `git diff` + `git diff --cached` 双重检查（捕获 staged 变更）
- 防止 LLM 评估器返回 convergence=0 触发假停滞

**commit:** `5d0029c` perf: pursuit convergence evaluation with programmatic floor

---

## 总览

| 版本 | 轮次 | Tokens | 耗时 | 成本 | 文件质量 |
|------|------|--------|------|------|----------|
| v1 基线 | 4 | 38,922 | 9min | $0.43 | 差 |
| v2 search/replace | 4-6 | 38k-72k | 14-27min | $0.4-0.9 | 好 |
| v3 路径修复 | 2 | 17,465 | 5.8min | $0.18 | 完美 |
| v4 thinking | 2 | 25,159 | 8min | $0.30 | 完美 |
| **v5 收敛底线** | **2** | **13,700** | **4.7min** | **$0.15** | **完美** |

**v1 → v5 改进：tokens -65%，耗时 -48%，成本 -65%，轮次 -50%**
