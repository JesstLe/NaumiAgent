# HAR-10.2b Pursuit Recovery Snapshot

## 目标

把 HAR-10.2a heartbeat、HAR-10.1 lease、HAR-10.4 checkpoint 和 HAR-10.5c reconcile 组合成一个只读、
有界、可跨前端复用的 `PursuitRecoverySnapshot`。用户在 Goal 页面和 Doctor 页面看到的是同一组权威事实，
不再把 Bridge ping、前端计时器或格式化文本当成 worker 健康。

本切片只提供观测与分类，不执行 resume、takeover、kill、retry 或修复动作。

## Typed contract

每个 snapshot 绑定稳定 `run_id`，包含：

- `recovery_state`：active/waiting/blocked/reconcile_required/orphaned/inconsistent/terminal/unknown；
- `heartbeat`：health、phase、instance、epoch、sequence、observed_at、timeout、age、detail code；
- `lease`：active/released/missing/error、owner、epoch、expiry、是否过期；
- `checkpoint`：ready/missing/error、checkpoint ID、sequence、phase、iteration、created_at；
- `reconcile_required` 与最近一条 typed reconcile evidence reason；
- 最多 8 条脱敏、可操作的 alerts。

Python producer 使用 frozen/extra-forbid Pydantic 模型；Node consumer 再次验证 schema、枚举、稳定 run link、
非负整数、heartbeat timeout、缺失 identity 不得夹带 owner、ready checkpoint 必须有 identity，并丢弃私有字段。
旧 goals/snapshot 客户端忽略可选 `recovery` 字段，schema_version 保持兼容。

## 机械分类

分类不调用模型，不解析中文状态：

- terminal run 默认 terminal，但仍持有 live lease 时为 inconsistent；
- `reconcile_required` phase 或 `action_inflight` checkpoint 优先标记需要核对；
- heartbeat instance/epoch 与 live lease owner/epoch 不一致为 inconsistent；
- heartbeat clock regression 为 inconsistent；
- running run 没有有效 live lease 为 orphaned；
- running run 持有 lease 但 heartbeat missing/error/stale/offline/stopped/failed 为 inconsistent；
- 证据一致的 running 为 active；waiting/blocked 保留其业务等待/阻塞状态；
- Store 缺失与读取失败分别投影 missing/error，不泄漏异常正文或本机路径。

Heartbeat 与 lease 是顺序事务，短暂可见性窗口不会被伪装成原子；刷新后仍不一致才具有诊断意义。

## 用户界面

- 新 UI Goal/Pursuit 页显示中文恢复状态、心跳健康、worker instance/sequence/age、lease owner/epoch/expiry、
  checkpoint sequence/phase、reconcile reason 和提醒；红/黄/绿只辅助文字语义；
- `/goal` CLI/TUI fallback 使用同一个 Python snapshot renderer，信息与新 UI 同源；
- `/doctor` 将当前 Goal 的 recovery snapshot 投影成稳定 `runtime-pursuit-recovery` health item，并参与整体
  severity；Bridge 心跳仍单独表示前端连接，不再冒充 worker 心跳；
- 没有当前 Goal/Pursuit 时 Doctor 不制造虚假的 recovery item。

## 验收证据

- 真实 Harness SQLite + PursuitStore 组合得到 active heartbeat/lease/checkpoint snapshot；
- running 且缺 lease 得到 orphaned；owner 不一致得到 inconsistent；
- action_inflight 与 reconcile evidence reason 得到 reconcile_required；
- checkpoint 读取失败返回 bounded error，不泄漏异常正文；
- Goal typed payload 与 Markdown fallback 消费同一 recovery；
- Bridge 真实 goals/snapshot 携带 Harness owner/instance；Doctor health 携带同源 recovery item；
- Node 严格拒绝 run ID 不一致、非法 timeout 与缺失 identity，私有字段不进入 state；
- 80/常用宽度页面通过既有 wrap/fit 管线，颜色关闭时仍有完整文字；
- 只运行 Goal/Doctor/Recovery Python 小模块和两个 Node 定向用例，不运行全量测试。

## 当前不足与下一切片

- 只聚合当前 Pursuit domain，没有 browser/background daemon/subagent/runtime producer；
- 只保存 latest heartbeat，没有历史丢包、jitter、crash-loop 或趋势；
- 当前只读页面没有 resume/takeover/cleanup 按钮；UI-18.3/18.5b 必须走 ToolExecution 权威路径；
- 多目标历史会逐项读取 Harness DB，当前上限 50；后续需要 batch read，而不是扩大无界查询；
- heartbeat/lease/checkpoint 来自不同事务，snapshot 是一致性诊断，不是分布式原子快照；
- orphaned/inconsistent 不自动改变 PursuitRun 状态，避免只读页面产生副作用。

下一最小切片应回到跨文档依赖表，优先评估 HAR-10.6a 结构化人工交互 authority 与 UI-18.4，或
HAR-10.3a 已交付内存队列的安全边界提升；后续应在 HAR-10.3b durable queue authority 与其他路线的最小
用户闭环之间重新比较依赖，不要继续线性扩张 recovery UI。
