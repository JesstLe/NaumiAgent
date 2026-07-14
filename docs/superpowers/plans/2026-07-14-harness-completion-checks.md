# Harness H3 Completion Contract + Check Runner 实施计划

## 状态与约束

- 日期：2026-07-14
- 状态：实施中
- 上游：H1 Profile/信任、H2 Knowledge Plane 已完成
- 范围：只实现 H3；Evidence Store、Eval、Feedback 和常驻控制面留给 H4-H7
- 验证：仅运行 H3 相关定向测试，不运行全量测试
- 提交：每个纵向切片独立验证、独立提交并立即推送 `main`

## Task 1：提取公共验证命令策略与进程执行器

**状态：已完成并推送。** 真实进程组 timeout/cancel、大输出 artifact、策略越界与
Workbench 兼容路径共 18 项定向测试通过。

**文件**

- 新建 `src/naumi_agent/validation/executor.py`
- 新建 `src/naumi_agent/validation/policy.py`
- 修改 `src/naumi_agent/workbench/validation.py`
- 新建 `tests/unit/test_validation_executor.py`
- 修改 Workbench validation 定向测试

**验收**

- 只接受 argv 数组，拒绝空参数、shell 元字符字符串接口和未知前缀；
- cwd 必须位于 workspace 或显式受管 worktree；符号链接越界必须失败；
- Windows 的 `python3` 映射到当前解释器；
- timeout/cancel 终止整个 process group，先温和终止，宽限后强杀并回收；
- stdout/stderr 有界返回，完整输出可由调用方写 artifact；
- 测试失败、超时、取消、策略阻止和基础设施失败使用不同状态；
- Workbench 继续写原有 ValidationRun/FailureCard，行为保持兼容。

## Task 2：实现受信任 Profile CheckRunner

**状态：已完成，等待本提交推送。** CheckRunner、Git tree fingerprint、success cache、
single-flight、Agent Tool、`/harness check` 与真实仓库小检查均已打通。

**文件**

- 新建 `src/naumi_agent/harness/checks.py`
- 新建 `src/naumi_agent/harness/fingerprint.py`
- 修改 `src/naumi_agent/harness/service.py`
- 修改 `src/naumi_agent/harness/tools.py`
- 新建 `tests/unit/test_harness_checks.py`

**验收**

- 未信任或执行中发生变化的 Profile 绝不启动进程；
- check id 必须来自当前 Profile，argv/cwd/timeout 不能被 Agent 覆盖；
- 按 `when_changed` 和 task kind 选择 required checks；
- 生成当前 Git tree fingerprint；成功结果仅在 `run_id + check_id + fingerprint` 一致时复用；
- 相同 fingerprint 的并发请求 single-flight，取消一个 waiter 不杀死其他 waiter；
- 用户 `/harness check <id>` 与 Agent `harness_run_check` 共享同一 service；信任仍只能由用户建立。

## Task 3：实现 Completion Contract、Gate 与 Receipt

**文件**

- 新建 `src/naumi_agent/harness/completion.py`
- 扩展 `src/naumi_agent/harness/models.py`
- 新建 `tests/unit/test_harness_completion.py`

**验收**

- task kind 为 `answer|analysis|change|monitor`；任何持久化工具会机械升级为 `change`；
- Contract 保留 objective、criteria、scope、required checks/evidence 和 source refs；
- Gate 检查 Todo、scope、当前 fingerprint 的 check、验证后修改和失败披露；
- 第一次缺证据返回一次明确纠正指令，第二次返回 `completed_unverified` 或 `blocked`；
- Receipt 只陈述可追溯事实，不把检查通过等同于业务形式化证明。

## Task 4：接入 Engine 与全部交互面

**文件**

- 修改 `src/naumi_agent/orchestrator/engine.py`
- 修改 CLI slash router/completer、Textual TUI、JSONL Bridge 与 Terminal UI 协议/渲染
- 新建/修改相应定向测试

**验收**

- run 开始后创建临时 Contract，不污染 `_full_history`；
- Tool 结果只向 Gate 提交规范化事实，不包裹或绕过 Tool execute；
- Todo reconciliation 后、final 发出前运行 Gate，最多纠正一次；
- Harness disabled/missing/untrusted 时保持现有行为；
- UI 显示 check 状态、fingerprint 和 verified/unverified receipt，状态不只靠颜色表达。

## Task 5：真实工作区 H3 验收与文档收口

**文件**

- 新建 `tests/integration/test_harness_h3_real_workspace.py`
- 更新 `.naumi/harness.yaml`
- 更新 `docs/harness/` 与 Harness 总设计状态

**真实场景**

1. 在临时 worktree 修改一个 Harness 小文件；
2. 先跳过 required check，Gate 必须请求一次纠正且不能产出 verified；
3. 通过共享 CheckRunner 跑定向测试；
4. 确认相同 tree fingerprint 得到 `completed_verified`；
5. 再改文件后旧检查立即失效；
6. 修改 Profile 后信任立即失效且命令启动数为零。

## 自审门禁

- bypass 只能省略人工二次确认，不能绕过 cwd containment、argv 结构或 Profile digest；
- 不使用 `shell=True`，不拼 shell 字符串，不信任 PATH 之外的仓库可写 shim；
- cancel/timeout 后不得遗留子进程；
- 输出、环境变量和错误中不得泄漏密钥；
- H3 不创建新的任务循环、Evidence 数据库或 Eval 框架；
- 所有用户可见失败均说明发生了什么、为什么、下一步怎么做。
