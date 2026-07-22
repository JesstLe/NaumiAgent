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
UI-17.2a-17.2e 已用共享 fixture 锁定 Bridge、TUI 与 Node reducer 的 runtime-health 八字段语义、权限脱敏、
bypass/session grant 四选择、模型主动询问和 canonical answer、submit/tool/receipt/cancel 基本运行生命周期，以及
Evaluation Lane RED/GREEN、资源证据与强制非最终边界，以及 token 合并、相关错误断流和发送 retry identity；
TUI 现在也能用 Ctrl+C 取消当前运行，空闲时不会误退出。
UI-14.1a 已建立 New UI/TUI 共用的严格 command index，现有补全可以展示参数 syntax、来源、category 和权限风险；
UI-14.2a 已进一步交付两端 `Ctrl+P` 命令 QuickOpen，支持别名、说明、类别、风险和 fuzzy 搜索，选择只填入 composer；
UI-14.2b 已增加本次启动内的隐私安全最近命令排序，只记录规范命令名且新启动重置；
UI-14.2c 已复用 UI-11 类型化任务快照增加两端任务 provider，`Tab` 切换并只填入只读详情命令；
UI-14.2d 已复用 ARC-03.2b2 工作区会话快照增加两端会话 provider，选择只填入 `/load <id>`；
文件/会话/Agent provider、跨启动历史与 Vim/input mode 尚未实现。
两端不再依赖各自的临时排队状态。
后续模块不得绕开这些路径重建新状态层。

## 未来顺序

UI-10/11/12/13 可按顺序独立交付；UI-14/15/16 可并行；UI-18 按 Goal/Pursuit 后端依赖分段推进；
UI-17 是统一发布门；17.1 manifest 已完成，17.2a runtime-health、17.2b permission/interaction 与
17.2c terminal run lifecycle、17.2d Evaluation Lane Receipt 与 17.2e stream recovery golden 已完成；17.3a 已验证
New UI 对旧 Bridge 的 Evaluation Lane typed→Slash 降级，17.3b 已建立发布合同驱动的通用
event-capability registry。仍需断连 uncertain/权限恢复 golden、未知关键事件分类和 17.3c-17.6。
