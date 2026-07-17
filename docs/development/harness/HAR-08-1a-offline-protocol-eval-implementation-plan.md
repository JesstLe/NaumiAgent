# HAR-08.1a 离线协议 Eval 实施计划

## 交付目标

交付一个真正可由用户和 Agent 使用的离线协议回归入口，而不是只新增数据类或 Prompt。

## 文件地图

| 文件 | 职责 |
| --- | --- |
| `src/naumi_agent/harness/eval_models.py` | 严格 Suite/Case/Result 契约 |
| `src/naumi_agent/harness/eval.py` | 有界 loader、fixture integrity、protocol runner、renderer |
| `src/naumi_agent/harness/service.py` | Profile allowlist 解析与统一服务入口 |
| `src/naumi_agent/harness/tools.py` | read-only `harness_eval` Tool |
| `src/naumi_agent/main.py` | `/harness eval` 共享命令表面 |
| `src/naumi_agent/cli/completer.py` | 子命令帮助元数据 |
| `.naumi/harness.yaml` | 声明仓库内置离线 suite |
| `docs/harness/evals/**` | 真实 suite 与六个 fixture |
| `tests/unit/test_harness_eval.py` | schema、loader、runner、预算、错误分类 |
| `tests/unit/test_harness_tools.py` | Agent Tool 契约 |
| `tests/unit/test_harness_surfaces.py` | slash 与 Tool 同源结果 |
| `tests/integration/test_harness_eval_real_workspace.py` | 当前仓库真实协议模块与无副作用验证 |

## Task 1：RED — schema 与 runner 行为

- 先写有效 suite、六类 fixture、畸形 schema、路径逃逸、digest mismatch、预算和分类测试；
- 运行 `tests/unit/test_harness_eval.py`，确认缺少模块而失败；
- RED 证据写回设计文档。

## Task 2：GREEN — 有界加载与纯静态执行

- 实现 frozen/extra-forbid Pydantic models；
- suite/fixture 全部用有界二进制读取，UTF-8、YAML/JSON 和 digest 分层报错；
- 实现 protocol runner adapter，直接调用生产协议代码；
- canonical result 排除 duration 后可稳定比较；
- 不捕获 `BaseException`，不执行任何外部命令。

## Task 3：GREEN — Service、Slash 与 Tool 双通道

- `HarnessService.eval_suites()` 只接受 Profile 声明 suite 的 id/path；
- `/harness eval [suite]` 调用 service 并渲染；
- `harness_eval` 调用同一方法，标记 read-only/concurrency-safe；
- 更新帮助和已有表面测试，不新增第二套路由。

## Task 4：真实仓库 fixture 与集成验收

- 创建六个带 digest 的 JSON fixture 与 suite；
- `.naumi/harness.yaml` 声明 suite；
- 连续运行两次，比较去 duration canonical result；
- 运行前后获取 tracked/untracked fingerprint，证明无工作区写入；
- 定向 Ruff、compileall、文档治理和小模块测试通过。

## Task 5：自审、文档与提交

- 检查是否复制协议逻辑、是否允许任意路径/命令、是否混淆 Eval error 与产品 failure；
- 记录未实现的 Replay/Sandbox/Live/Baseline/Comparator/Store；
- HAR-08 标记 `partial (8.1a)`，不宣称整体完成；
- 独立英文 commit，确认 main 与 origin/main 同步。

## 明确禁止

- 不运行全量测试；
- 不自动信任 Profile 或执行 Profile checks；
- 不把 fixture expected 当生产逻辑；
- 不保存原始用户对话、secret、reasoning 或源码正文；
- 不新增 eval SQLite 表（文档规定 H5 后再迁移）。

## 完成状态

- Task 1：完成；RED 以缺少 `naumi_agent.harness.eval` 模块的 collection error 固定。
- Task 2：完成；strict/frozen schema、有界读取、digest、预算与生产 protocol runner 已实现。
- Task 3：完成；Service、共享 slash router 和 read-only Agent Tool 使用同一路径。
- Task 4：完成；仓库内置六个 fixture 连续运行一致，真实工作区源文件 digest 前后不变。
- Task 5：完成；自审、当前架构、债务、README 与模块注册表已同步。
