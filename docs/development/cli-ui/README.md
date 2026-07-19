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
durable queue、连续 claim 和 `/send-now` parity；两端不再依赖各自的临时排队状态。
后续模块不得绕开这些路径重建新状态层。

## 未来顺序

UI-10/11/12/13 可按顺序独立交付；UI-14/15/16 可并行；UI-18 按 Goal/Pursuit 后端依赖分段推进；
UI-17 是统一发布门。
