# HAR-08 H5a 不可变 Eval Result Store

## 1. 目标

把每次 `HarnessEvalSuiteResult` 作为可复核的 typed sample 写入现有 `HarnessStore`，为统计比较、
Baseline promote、HAR-09 outcome tracking 和 EVO-03 evaluation receipt 提供同一持久化事实源。

本切片只保存原始 Eval sample；不创建 Baseline 指针、不保存 Comparison verdict、不自动晋升，也不新增
平行数据库。

## 2. Schema v8

`harness_eval_results` 使用以下不可变键：

- `workspace_root`：规范化绝对工作区边界；
- `batch_id`：1..128 字符的安全 cohort 标识；
- `suite_id`：typed Result 自带 Suite ID；
- `sample_index`：0..9999 的组内序号。

四者生成稳定 record ID，并有数据库唯一约束。每行同时保存：Identity SHA-256（允许评测错误时为空）、
Result SHA-256、完整 typed JSON 和带时区创建时间。按 workspace/batch/suite/sample 建索引，统计层无需
扫描其他工作区或其他 cohort。

迁移从任意旧版 additive 建表并把 `PRAGMA user_version` 提升到 8；重复初始化不覆盖旧记录。

## 3. 写入语义

- 第一次写入使用 `BEGIN IMMEDIATE` 原子提交；
- 同一不可变键、同一 Result digest 的重试返回原记录，调用方的新时间戳不会改写事实；
- 同一键写入不同 Result 抛出 `HarnessStoreConflictError`；
- Result 先经过 Pydantic 复核和统一 OutputGuardrail 脱敏，再计算摘要并保存；
- 单条 typed JSON 上限 4 MiB，大型产物后续只允许保存 URI/digest；
- batch ID、sample、limit、时间戳和 Result 类型均在打开事务前验证。

## 4. 读取与防篡改

`get_eval_result()` 和 `list_eval_results()` 都要求精确 workspace/batch/suite。列表按 sample index 升序、
默认最多 100、硬上限 10000。

每次反序列化都会复核：

1. Result JSON SHA-256；
2. Pydantic typed schema；
3. 行内 Identity SHA 与 Result Identity；
4. stable ID 与 workspace/batch/suite/sample；
5. Suite ID 与 Result 内容；
6. 带时区 created_at。

任一不一致都作为 Store 损坏处理，不把伪造数据送入 Comparator。

## 5. 已验证场景

- 相同 sample 重试幂等，创建时间保持首次事实；
- 同键不同 duration/内容不可覆盖；
- 关闭后由新 Store 实例恢复完全相同 typed Result；
- 乱序写入 0/1/2 后按 sample 顺序读取并受 limit 控制；
- 相同 batch 名在其他 workspace 不可见；
- v7 数据库 additive 迁移到 v8，重复初始化稳定；
- 直接篡改 result JSON 后读取失败；
- 非法 batch ID、bool/越界 sample、非法 limit 被拒绝；
- `api_key=...` 等敏感 message 在写盘前脱敏。
- 真实 Git 工作区 production hello Suite 连续运行 5 次，五个 Identity-bound Result 写入后由新 Store
  恢复，并直接交给 8.7d Comparator 形成 unchanged 结论。

## 6. 后续

- H5b 已完成：不可覆盖 Baseline version、active selector、审计事件与 promote eligibility；
- H5c：机械/Policy/统计 Comparison receipt，只引用两组 sample 与 digest；
- HAR-08.8：Slash/Tool/API/New UI 查询、比较和显式 promote；
- HAR-06：把 Eval sample 纳入 session/workspace 生命周期与 retention 预览。
