# HAR-08.8b 重复 Eval Candidate Batch

## 1. 目标

把 8.7d 的重复运行器与 H5a Result Store 接入正常用户/Agent 工作流，使 Candidate cohort 不再只能
由测试或内部 Python API 产生。本切片只生成并保存 batch，不晋升 Baseline、不切 selector、不自动比较。

## 2. 入口与权限

- 用户：`/harness eval <suite> --repeat 5 [--batch <id>]`；
- Agent：`harness_eval_batch(suite, repetitions=5, batch_id?)`；
- Service：`HarnessService.eval_repetition_batch()`；
- 普通 `/harness eval [suite]` 与 `harness_eval` 保持单次、read-only；
- batch Tool 标记为非 read-only，因为它追加用户状态库，但不修改工作区文件、Fixture 或 Profile。

两种入口共享 Service、typed status 和 renderer。重复次数必须是 5..100；batch ID 可省略并由 UTC 时间与
随机后缀生成，也可显式提供 1..128 字符安全 ID。

## 3. 运行与持久化

1. 在运行前验证 suite/repetitions/batch ID 和 Harness Store；
2. 从当前 Profile 只解析一个精确声明的 Suite ID 或相对路径，`all` 不允许进入重复 batch；
3. 使用一次 Git source identity 边界运行全部 repetitions，避免每个 sample 捕获不同源码状态；
4. 每个 Result 继续携带 Suite/Profile/Runner/Policy/平台 Identity；
5. 按 sample index 0..N-1 调用 H5a immutable write，保存脱敏 typed JSON 和 digest；
6. 完成时显示 requested/completed/persisted、batch、Suite、Identity、资格与耗时。

预算耗尽产生 `partial`：已经完成的样本保留，但文案明确说明不能晋升或形成统计结论。Store 冲突或损坏
产生 `error`，不覆盖既有样本，并要求用新 batch ID 重试。

## 4. 边界

- 未配置/无效 Profile 不运行；
- 未声明或不唯一的 Suite 不运行；
- batch ID 在昂贵运行前校验；
- 重复运行不调用模型、不访问网络、不执行工作区命令；
- Profile 未受信任时仍允许离线采样，但 Identity 为不可晋升；
- 本切片不保证整个 cohort 单事务写入；发生基础设施故障时已保存的 immutable prefix 保留，后续晋升
  gate 会因 sample count/index/Identity repetitions 不完整而拒绝。H5a 后续可增加事务批写优化。

## 5. 验收

- 真实临时 Git 工作区的 production hello Suite 由 Slash 连续运行 5 次并保存 sample 0..4；
- Agent Tool 用第二个 batch 重复同一真实链路并保存 5 次；
- 两组各自 Identity digest 唯一，Result 均包含 typed Baseline Identity；
- Slash/Tool 文案显示完成 5/5、已保存 5、batch ID 与晋升资格；
- 4/101 repetitions、非法/空 batch、缺少 Suite、未声明 Suite 在运行或写入前拒绝；
- 原单次 Eval、Baseline 状态、H5a/H5c Store 与 Bridge command discovery 定向测试保持通过。

## 6. 后续

- HAR-08.8c 已完成：对完整、eligible batch 做显式 promote，要求固定 actor/reason；
- HAR-08.8d：Candidate 与 active Baseline 生成 H5c Comparison receipt；
- HAR-08.8e：typed New UI/TUI batch progress 与 receipt detail；
- H5a batch transaction：在不牺牲 immutable prefix recovery 的前提下定义原子 cohort API。
