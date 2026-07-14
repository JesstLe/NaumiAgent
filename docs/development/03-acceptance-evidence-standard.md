# 验收与证据标准

## 1. 验收等级

| 等级 | 含义 | 必需证据 |
| --- | --- | --- |
| A0 | 规格完整 | 接口、状态、错误、非目标、依赖、验收用例 |
| A1 | 单元正确 | RED/GREEN、边界和错误路径 |
| A2 | 子系统集成 | 真实 Store/Bridge/Tool/Renderer 链路 |
| A3 | 用户场景 | 非 mock 触发到结果、取消、恢复、回执 |
| A4 | 平台发布 | OS/终端/打包/升级/回滚矩阵 |
| A5 | 长期可靠 | 压测、故障注入、SLO、可观测与灾难恢复 |

普通模块至少达到 A3；发布、daemon、集群、自进化提升至少达到 A4；HAR-10、ARC-06、
ARC-08 和 EVO-06 必须达到 A5。

## 2. 测试矩阵

- Unit：纯规则、Schema、状态机、格式器，不依赖网络。
- Contract：Python/Node/TUI、Store schema、Tool schema、事件版本一致。
- Integration：真实 SQLite、真实 Git 临时仓库、真实子进程或 Bridge。
- E2E：用户入口、权限选择、长任务、取消、恢复、错误和回执。
- Compatibility：macOS/Linux/Windows、TTY/非 TTY、窄宽屏、Unicode/emoji。
- Performance：事件吞吐、首帧、滚动、内存、并发、公平性和背压。
- Security：路径越界、权限绕过、secret 泄漏、重放攻击、跨工作区探测。

## 3. 真实场景规则

真实场景不能只 mock 目标模块。允许 mock 外部付费模型，但必须运行真实内部边界，例如：

- Harness：真实 Store、Git fingerprint、Check subprocess、跨实例恢复。
- UI：真实 Python JSONL Bridge 子进程与 Node renderer。
- Daemon：真实本地 socket/pipe、进程崩溃和重连。
- 自进化：真实临时 worktree、真实补丁、真实定向测试、真实 rollback。

## 4. 性能基线

各模块文档可收紧但不能放宽以下默认门槛：

- 控制事件 P95 处理延迟小于 100ms；高频 token/event 必须合并或背压。
- 终端 resize/scroll 不阻塞输入超过 100ms；10k 消息内存有明确上限。
- heartbeat 三个周期未响应才判失联，避免单次抖动误杀。
- 取消请求 1s 内可见，5s 内进入终态或明确显示正在强制终止。
- Store 写入失败不得丢失主任务结果；必须产生去重基础设施警告。

## 5. 完成回执

回执至少列出：改动、检查、真实场景、审批、风险、未验证项、下一步、commit。任何
“部分完成”必须显式标记，不能仅在自然语言正文里弱化。
