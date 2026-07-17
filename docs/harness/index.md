# NaumiAgent Harness 知识地图

> H4.5 之后的开发模块、交接和验收见 `docs/development/harness/README.md`；自进化闭环见
> `docs/development/self-evolution/README.md`。

本文是仓库 Harness 的权威入口。它描述已经存在的知识、检查、Completion Gate、安全 Replay，
以及 HAR-08.1a、HAR-08.6a/8.6b、HAR-08.7a-8.7c 已落地的离线协议 Eval、
Baseline Identity 与比较策略；未来阶段记录在
[debt.md](debt.md)，不会伪装成已实现功能。

## 快速导航

| 任务 | 首选文档 | 真实代码入口 |
|---|---|---|
| 理解 Profile、信任与诊断 | [architecture.md](architecture.md) | `src/naumi_agent/harness/profile.py`、`trust.py`、`service.py` |
| 理解仓库知识选择 | [architecture.md](architecture.md) | `src/naumi_agent/harness/knowledge.py`、`context.py` |
| 运行受信任检查 | [architecture.md](architecture.md) | `src/naumi_agent/harness/checks.py`、`fingerprint.py`、`validation/` |
| 运行与比较离线协议评测 | `evals/protocol-hello-core.yaml` | `src/naumi_agent/harness/eval.py`、`eval_models.py`、`eval_identity.py`、`eval_compare.py`、`eval_suite_compare.py`、`eval_policy.py` |
| 修改 Agent 临时上下文 | [golden-principles.md](golden-principles.md) | `src/naumi_agent/orchestrator/context_assembly.py`、`engine.py` |
| 修改 Terminal UI | `docs/product/terminal-ui/` | `frontend/terminal-ui/src/`、`frontend/terminal-ui/test/` |
| 修改 Mac Workbench | `apps/macos/NaumiAgentWorkbench/README.md` | `apps/macos/NaumiAgentWorkbench/Sources/` |
| 查看未实现阶段与限制 | [debt.md](debt.md) | `docs/superpowers/specs/2026-07-14-harness-engineering-design.md` |

## 当前事实

- Profile 固定在 `.naumi/harness.yaml`，只保存机械配置，不保存 API Key。
- Profile 必须通过严格解析，并由用户信任其精确 SHA-256 digest；内容变化立即失信。
- 信任记录位于用户状态目录，不进入仓库，也不会暴露为 Agent 写工具。
- H2 使用路径、文件名、文本、import、Git changed paths 和 source-test 关系进行确定性选择。
- L0 默认最多 1,000 tokens；L1 默认最多 8,000 tokens；总量还受模型窗口 15% 和 12,000 硬上限限制。
- 仓库知识只进入带 `<naumi_harness_context>` 标记的当前轮 system snapshot，不进入 `_full_history`。
- `harness_read_knowledge` 与 `/harness knowledge` 共用 `HarnessService.read_knowledge()`。
- 受信任 Profile 中的精确检查可由 `/harness check <id>` 或 `harness_run_check` 按需执行。
- Profile 声明的离线 Suite 可由 `/harness eval [suite]` 或 `harness_eval` 运行；静态 runner
  不要求信任，不执行模型、命令、网络或写操作，fixture 使用 SHA-256 锁定。
- 显式 `/harness eval <suite> --repeat 5 [--batch <id>]` 或 `harness_eval_batch` 重复运行一个
  Suite，并只向用户状态库追加 immutable Candidate samples；新 UI 显示真实逐样本评测/保存进度，
  Textual TUI 同步状态栏。它不会修改工作区或晋升 Baseline。
- `/harness baseline <suite>` 与 `harness_eval_baseline` 只读展示 active Baseline 和最近权威
  Comparison receipts；新 UI 使用 typed、可滚动的彩色状态页，Textual TUI/兼容终端复用同一
  Service 状态模型。它们不会运行 Eval、晋升版本或改变 selector。
- `/harness baseline promote <suite> <batch> --reason <原因>` 与
  `harness_eval_baseline_promote` 显式调用同一 H5b gate；actor 由入口固定，旧版本重试不会回拨。
- `/harness baseline compare <suite> <candidate-batch>` 与 `harness_eval_compare` 使用 active Baseline
  生成 H5c 权威 receipt；相同比较幂等，selector 竞态会显式标记。
- 离线 Eval 明确区分生产实现回归与 Suite/fixture 自身错误；当前内置六个 hello 协商 fixture。
- 可比较结果绑定 Git source identity、Profile/Suite/fixture/runner/policy digest；工作树不干净、
  source 变化或证据不完整时不会伪装成可比较 baseline。
- 比较分三层：身份兼容性、逐 Case 机械差异、门槛与 guardrail 策略；Suite 错误或跳过始终是
  `inconclusive`，不能被宽松阈值掩盖。
- 检查结果绑定 run id、Profile digest 与 Git tree fingerprint；并发相同检查 single-flight，
  Profile/工作树变化会阻止缓存复用或使运行结果失效。
- Completion Contract/Gate 已接入同步与流式 Engine final，机械区分 verified、unverified、
  blocked，最多要求一次纠正；流式路径不会先泄露被 Gate 拒绝的完成文本。
- 新 UI、Textual TUI 与 CLI 共用 Harness 回执事件；状态、检查、变更文件与 tree fingerprint
  都有文字表达，颜色仅作辅助。

## 新鲜度记录

```yaml
source_paths:
  - src/naumi_agent/harness/**
  - src/naumi_agent/orchestrator/context_assembly.py
  - src/naumi_agent/orchestrator/engine.py
  - .naumi/harness.yaml
verified_on: 2026-07-14
verified_at_commit: c444150b080f6372368f14106fc31e828db1e815
evidence: docs/superpowers/plans/2026-07-14-harness-completion-checks.md
```

`verified_at_commit` 指向包含 H3 Engine/Gate/UI 生产代码与定向测试的 feature commit；
后续证据提交使用 detached 临时 worktree 验证了纠正、verified、stale 与 Profile 失信路径。
