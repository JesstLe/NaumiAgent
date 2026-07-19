# Harness 当前架构

## 目标

Harness 负责把“仓库事实、用户意图、执行环境和验证边界”组织成 Agent 可可靠消费的工程上下文。
Profile/Trust/Knowledge 提供确定性证据，CheckRunner 与 Completion Contract/Gate/Receipt
约束完成真实性，持久 Store 支持 Explain/Replay；离线 Eval 使用真实生产协议实现做机械回归。

## 模块所有权

| 模块 | 单一职责 |
|---|---|
| `models.py` | 严格、冻结的 Profile 数据契约 |
| `profile.py` | 有界 YAML 读取、路径约束、精确 byte digest |
| `trust.py` | 工作区 + Profile digest 的用户级 SQLite 信任记录 |
| `knowledge.py` | 安全发现、Git 状态、文件 fingerprint、确定性排序、L2 读取 |
| `context.py` | L0/L1 渐进披露、模型窗口预算、闭合 Markdown 证据块 |
| `fingerprint.py` | HEAD、index、dirty/untracked bytes 的 Git tree fingerprint |
| `checks.py` | segment-aware changed-path 匹配、required check 选择、受信任执行、success cache 与 single-flight |
| `completion.py` | task kind 升级、scope/Todo/check/evidence Gate 与结构化 Receipt |
| `eval_models.py` | 严格、冻结的离线 Suite/Case/Result/Policy 契约 |
| `eval.py` | 有界 Suite/fixture 读取、integrity 校验、guardrail 证据与生产协议静态 runner |
| `eval_identity.py` | 绑定 source/Profile/Suite/fixture/runner/policy 的 Baseline Identity |
| `eval_compare.py` | 身份兼容性检查；拒绝跨配置、跨 runner 或跨 policy 误比较 |
| `eval_suite_compare.py` | 逐 Case 机械差异与 Suite 不稳定性判定 |
| `eval_policy.py` | pass rate、回归数、实现失败与 guardrail 门槛判定 |
| `validation/` | Workbench/Harness 共用 argv policy、cwd containment 与进程组执行器 |
| `service.py` | 信任门、知识/检查并发缓存、统一用户/Agent facade |
| `tools.py` | 状态、诊断、解释、Replay、Eval、知识只读 Tool 与一个检查 Tool；不包含 trust/untrust |

Harness 包不得反向 import `AgentEngine`。Engine 只持有一个 `HarnessService`，在 run 开始创建临时 Contract，并在每轮模型调用前请求当前任务的知识 bundle。

## 数据流

```text
latest user task
  -> HarnessService.status()
  -> exact Profile digest trusted?
       no  -> no repository body
       yes -> RepositoryKnowledgeIndex
                -> canonical path + exclusion + bounded UTF-8 read
                -> Git HEAD / changed paths
                -> deterministic rank
             -> HarnessKnowledgeContextComposer
                -> L0 manifest
                -> L1 bounded evidence blocks
  -> existing Harness runtime snapshot
  -> AgentEngine._messages only
```

L2 不会自动塞进上下文。Agent 调用 `harness_read_knowledge`，或用户执行 `/harness knowledge` 后，Service 会复用同一索引与安全读取逻辑。

离线 Eval 路径是：Profile `evals.suites` allowlist → 有界 YAML schema → fixture 路径与 SHA-256
校验 → 调用生产 `ui.protocol` hello 标准化/协商 → expected/actual 机械比较 → Git source
稳定性与 no-side-effect guardrail 验证 → 生成绑定 policy digest 的 Baseline Identity。比较时先做
身份兼容性，再做逐 Case 机械差异，最后执行 threshold/guardrail policy；Suite error/skip 或证据缺失
只会得到 `inconclusive`。用户命令和 `harness_eval` Tool 共用 `HarnessService.eval_suites()`；整个路径
不经过 ModelRouter、ValidationExecutor 或 Store。

完成路径是：run 开始绑定 Profile digest 与初始 tree fingerprint → Todo 对账 → Gate 机械选择
当前 task kind/changed paths 的 required checks → 缺证据时隐藏模型的提前完成文本并要求一次纠正 →
当前 run/profile/tree 的检查通过后发出 `completed_verified`。新 UI、Textual TUI 与 CLI 共用类型化
system notice；绿色、黄色、红色分别辅助表达 verified、unverified、blocked，正文仍明确写出状态、
检查、变更文件和 fingerprint。

验证路径是：精确 Profile digest 信任 → check id 查表 → argv/cwd 机械策略 → 共享
ValidationExecutor → 执行后再次校验 Profile 与 tree fingerprint。Agent Tool 还要先通过现有
PermissionChecker；bypass 可免人工确认，但不能绕过 Profile 信任、argv 或 cwd 边界。

## 信任与缓存

1. 每次知识调用先重新加载 Profile 并读取用户级 Trust Store。
2. 缓存键包含工作区、Profile digest、Git HEAD、changed paths 与文件 digest。
3. 每次命中检查已知候选的大小与 mtime；被复用的 L1 source 还会重新计算精确 bytes digest。
4. Git HEAD/changed paths 最多每 30 秒重新审计一次；NaumiAgent 内成功的写工具会立即使索引失效。
5. 并发 miss 共用一个 build task；相同 snapshot、任务与模型窗口复用最多 16 个进程内选择结果。
6. build 和摘要失配后的重建均再次读取 Profile/Trust；组装期间发生变化时丢弃结果。
7. Trust Store 损坏、Git 不可用或索引失败不会中断主任务，但绝不会降级为“默认信任”。

## 发现与选择

发现顺序遵循：root `AGENTS.md`、目标路径祖先链的嵌套 `AGENTS.md`、Profile entrypoints、构建清单、include 源码/测试。更具体的 `AGENTS.md` 只作用于其目录后代。

排序只使用可复验信号：精确路径、文件名/stem、路径 token、文本/符号命中、import、source-test 配对、Git changed、entrypoint/build fallback。相同得分按 POSIX 相对路径排序，因此相同输入产生相同结果。

## 安全边界

- 不跟随越出工作区的 symlink。
- 不读取目录、设备、socket、无权限、超限或非 UTF-8 文件。
- `.env`、凭据名、私钥后缀、VCS/cache/runtime 目录默认排除。
- 图片、压缩包、日志、完整 diff、二进制和 base64 载荷只保留结构化警告，不进入模型上下文。
- Git 使用 argv、`shell=False` 和 timeout。
- Profile checks 只允许按 id 执行，调用方不能覆盖 argv、cwd 或 timeout。
- Profile check 可声明 `provides: [lint, compile, unit, contract, smoke]`；该字段只描述真实能力，供
  EVO-03 做逐 changed-path verifier binding，不授予额外执行权限。
- Eval Tool 只允许 Profile 声明的 Suite id/路径，不能传入命令、provider、cwd 或任意 fixture。
- 非 Git 工作区、Profile 变化或 fingerprint 基础设施失败会阻塞 verified，不静默降级。
- timeout/cancel 终止整个进程组；测试失败、基础设施失败、策略阻止和 stale 分开报告。
- 原始输出在执行器内有界；用户/模型渲染前再次裁剪、脱敏并移除终端控制字符。
- Profile check 可另行声明 `adversarial_probes: [boundary, concurrency, security, recovery,
  cross_platform, reward_hacking]`。该字段默认空且只描述 check 已真实覆盖的对抗维度；普通 `unit/contract`
  不会自动获得对抗能力，修改标签会改变 Profile digest 并要求重新信任。
