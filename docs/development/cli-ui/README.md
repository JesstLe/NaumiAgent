# CLI/TUI/New UI 后续模块册

## 产品边界

- `naumi` 默认启动 `frontend/terminal-ui` 新 UI。
- Textual TUI 是可靠 fallback，必须保留核心任务能力。
- 旧 prompt_toolkit CLI 代码保留但 deprecated，不再接受独占新功能。
- Python Bridge 拥有运行/权限/任务事实；Node UI 只拥有焦点、折叠、滚动等本地状态。

## 当前已完成地基

UIMessage、JSONL protocol、tool/activity card、semantic rendering、completion receipt、runtime
inspector、agent control center、tasks、permissions、history、heartbeat、working animation、跨平台
启动、类型化 Goal/Pursuit 只读页、New UI/Textual TUI 共用的 durable interaction authority，以及
UI-15.1a 的 New UI 有界 stream delta 合并与控制事件绘制屏障已存在。HAR-10.3b4 已补齐 TUI 运行中输入的
durable queue、连续 claim 和 `/send-now` parity；HAR-10.3b5 已增加双端 `/cancel-queued` 和明确的未派发取消
状态。UI-16.6a 已让 working indicator 重新显示受限、脱敏的运行性能阶段，同时避免等待态展示过时指标。
UI-13.1c 与 ARC-01.4c1-4c3 已让 New UI/TUI Doctor 都展示各自 terminal lifecycle 的实时 retention 状态；缺少
Composition 注入时明确标记不可观测，而不是伪造调度健康。UI-17.1 已为两端发布 14 项严格 capability manifest；
UI-13.2a 已让 Provider 本地配置与显式 live probe 失败产生低基数稳定诊断码，并由 New UI/TUI/CLI 共用；
UI-13.5a 已让 New UI/TUI/CLI/Agent Tool 先预览固定 3 文件的脱敏诊断 ZIP，再以精确 Snapshot 摘要把
同一 Bundle 原子写入平台 Naumi 状态目录；包不含聊天、reasoning、raw trace、环境变量全集、凭据或源码；
UI-17.3c 已进一步让 New UI 在旧 Bridge 不声明 `doctor_export` 时保留 Health 页面、显示兼容提示并阻止
preview/write 发送，Python Bridge 也会在 Plan/写盘之前拒绝未协商请求；
UI-17.2a-17.2f 已用共享 fixture 锁定 Bridge、TUI 与 Node reducer 的 runtime-health 八字段语义、权限脱敏、
bypass/session grant 四选择、模型主动询问和 canonical answer、submit/tool/receipt/cancel 基本运行生命周期，以及
Evaluation Lane RED/GREEN、资源证据与强制非最终边界，以及 token 合并、相关错误断流、发送 retry identity 和
New UI/Textual 生产渲染路径的固定视口 ANSI/text capture；
TUI 现在也能用 Ctrl+C 取消当前运行，空闲时不会误退出。
HAR-07.5c1 已让 New UI/TUI/CLI 共享 `/copy receipt [receipt-id|latest]`，从当前 session 的持久
Completion/Harness authority 生成同一有界、脱敏文本并保存到 `.naumi/exports`；完成卡显示精确入口，
剪贴板不可用时仍保留文件。
UI-14.1a 已建立 New UI/TUI 共用的严格 command index，现有补全可以展示参数 syntax、来源、category 和权限风险；
UI-14.2a 已进一步交付两端 `Ctrl+P` 命令 QuickOpen，支持别名、说明、类别、风险和 fuzzy 搜索，选择只填入 composer；
UI-14.2b 已增加本次启动内的隐私安全最近命令排序，只记录规范命令名且新启动重置；
UI-14.2c 已复用 UI-11 类型化任务快照增加两端任务 provider，`Tab` 切换并只填入只读详情命令；
UI-14.2d 已复用 ARC-03.2b2 工作区会话快照增加两端会话 provider，选择只填入 `/load <id>`；
UI-14.2e 已增加 Engine-owned、可取消、workspace 隔离、最多 100k 文件的后台索引，两端文件 provider
选择只填入安全 `/read` 模板；
UI-14.2f 已复用 Agent Control schema v2 one-shot snapshot 增加两端 Agent provider，选择只填入
`/agents agent <name>` 并在显式提交后定位详情；
UI-14.2g 已增加 surface-aware 权威页面索引，两端页面 provider 只填入精确导航命令；
跨启动历史、typed argument form 与 Vim/input mode 尚未实现。
两端不再依赖各自的临时排队状态。
后续模块不得绕开这些路径重建新状态层。

## 未来顺序

UI-10/11/12/13 可按顺序独立交付；UI-14/15/16 可并行；UI-18 按 Goal/Pursuit 后端依赖分段推进；
UI-17 是统一发布门；17.1 manifest 已完成，17.2a runtime-health、17.2b permission/interaction 与
17.2c terminal run lifecycle、17.2d Evaluation Lane Receipt、17.2e stream recovery 与 17.2f terminal capture 已完成；17.3a 已验证
New UI 对旧 Bridge 的 Evaluation Lane typed→Slash 降级，17.3b 已建立发布合同驱动的通用
event-capability registry，17.3c 已完成 Doctor Export 的失败关闭降级。仍需断连 uncertain/权限恢复 golden、
未知关键事件分类和 17.3 其余治理、17.4-17.6。
