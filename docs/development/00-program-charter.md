# NaumiAgent 后续开发总纲

## 1. 北极星目标

NaumiAgent 最终应成为可持续运行、可解释、可审计、可并发、可跨平台、可安全自我改进
的通用编码 Agent。产品体验要达到 Codex/Claude Code 一线终端产品水平，后端 Harness
必须使“完成”成为有证据的工程状态，自进化必须成为可回滚的受控实验而不是直接改写
生产源码。

## 2. 五个项目域

1. `HAR` Harness Engineering：契约、证据、回放、评测、反馈和长周期控制。
2. `UI` CLI/TUI/New UI：默认新 UI、TUI fallback、稳定协议与跨终端体验。
3. `CC` Claude Code Source Alignment：合法、可追踪地吸收本地源码中的成熟机制。
4. `ARC` Future Architecture：解耦 core/runtime/tools/frontends/daemons，支持演进部署。
5. `EVO` Self-Evolution：以 Harness 和 Eval 为裁判的闭环自我改进。

## 3. 阶段门

| 阶段 | 进入条件 | 退出条件 |
| --- | --- | --- |
| P0 文档冻结 | 模块 ID、依赖、接口已定义 | 无冲突命名、无模糊验收、审核人确认 |
| P1 单模块实现 | 上游依赖 implemented | 定向单测、lint、compile、真实 smoke 通过 |
| P2 协议集成 | 两个以上模块需要联调 | 契约测试、断序/重试/兼容路径通过 |
| P3 产品验收 | 主路径完成 | macOS/Linux/Windows 矩阵与恢复场景通过 |
| P4 自进化准入 | Harness/Eval/回滚可靠 | 变异不直接触碰主分支，提升有统计证据 |

## 4. 绝对工程原则

- 一个模块一个提交；模块内部子模块可以多次 RED/GREEN，但不跨模块顺手改造。
- 新能力同时提供用户表面与 Agent Tool，二者共享底层 service；纯 UI 能力除外。
- 新 UI 是默认入口，TUI 是 fallback，旧 CLI 代码保留但标记 deprecated。
- bypass 表示权限全通过，但仍保留审计、预算、资源上限和不可破坏系统边界。
- Store 中不保存 secret、认证 header、完整 stdout、模型 reasoning 或大二进制正文。
- 所有长任务支持心跳、取消、超时、背压、恢复和幂等终态。
- 所有用户可见错误使用中文，包含发生了什么、为什么、下一步。
- 不用全量测试替代模块验证；全量测试只在阶段门和发布候选运行。

## 5. Definition of Done

一个模块只有同时满足以下条件才是 `implemented`：

- 文档中所有必需子模块已实现，非目标没有被偷偷扩入。
- 目标接口有严格类型/Schema，错误状态可枚举，不以自由文本作为控制协议。
- 单元、集成、契约、真实场景中适用的层级均有新鲜证据。
- macOS/Linux/Windows 或明确适用的平台矩阵通过；跳过项必须写原因。
- 安全、隐私、并发、恢复、空输入、极端参数和失败路径已审查。
- 用户能从触发到结果完整操作；状态、进度、取消和回执可见。
- 文档、注册表和代码状态同步，commit message 使用英文。

## 6. 明确不接受

- Prompt 套壳工具、只测 import、mock-only E2E、未验证的“应该可用”。
- 为未来模块预建空类、空数据库表或无调用入口的死代码。
- 复制 Claude Code 组件却丢失来源、许可证证据或行为回归测试。
- 自进化直接修改当前用户工作树、覆盖未提交改动或自动推送主分支。
