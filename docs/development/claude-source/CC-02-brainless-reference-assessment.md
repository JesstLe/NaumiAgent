# CC-02 外部参考评估：theswerd/brainless

## 结论

决策：`reference`，不作为 NaumiAgent New UI/TUI 的 renderer 依赖，也不改变 CC-02 对 React/Ink 的
独立可替换性实验。

评估快照：2026-07-22 检查并于 2026-07-23 复核 `main@4c5d5ab` 未变化。仓库仅 8 个提交、没有正式
release，仍属于快速演进的早期项目。

## 它是什么

`brainless` 是一个 shadcn/ui registry，用可复制的 React DOM 组件复刻 Claude Code、Codex 和 Grok 的
终端外观，目标场景是文档、Demo、营销页和产品 Web UI。它提供 header、message、thinking/working、
tool/exec、diff、permission、prompt、slash menu、todo 等组件，以及组合后的 session block。

其运行栈包括 Next.js、React DOM、shadcn、Radix UI、Tailwind、Shiki 与 cmdk。它不是 Ink renderer，
也不是 ANSI/PTY runtime。

## 对 NaumiAgent 有价值的部分

### 1. 视觉信息架构参考

- 对话、思考、工具、diff、权限和任务被拆为独立组件，适合核对 Naumi 的 typed message taxonomy。
- Claude/Codex 两套视觉语言可以帮助区分“共享语义”与“主题表现”，避免把颜色和边框写入后端协议。
- permission、slash menu、todo 等交互态可作为截图级 UX 验收样本，而不是作为运行时 authority。

### 2. 真实终端捕获方法

仓库用 tmux 驱动真实 CLI，并同时保存 ANSI、HTML 和纯文本帧。这个方法可用于补强 Naumi UI-16/17：

- macOS/Linux 下捕获固定终端尺寸的 golden frames；
- 对 New UI 与 TUI 的同一 typed event fixture 做并排比较；
- 区分语义回归、ANSI 色彩回归和终端宽度回归；
- Windows 采用 ConPTY/Windows Terminal 等价 harness，而不是假设 tmux 可移植。

这套捕获思路应由 Naumi 自己实现并接入现有 renderer benchmark，不能直接依赖其脚本作为发布门。

### 3. 可选的 Web 展示层

若未来提供浏览器中的会话回放、文档演示或产品官网，可在许可证审查后选择性适配其 Web 组件。
这属于 Web surface，不应反向污染 JSONL 协议、Node terminal renderer 或 Textual TUI。

## 不直接采用的原因

- DOM/CSS 布局不能替代终端的 cell width、ANSI capability、PTY resize、IME、raw input 和信号处理。
- 引入 Next/React DOM/Tailwind/Radix/Shiki 会显著扩大 CLI 安装体积与供应链面，却不能解决核心终端能力。
- 仓库没有 release，提交历史短，不适合作为 Naumi 默认 CLI 的关键依赖。
- 组件忠实复刻外观不等于拥有 Naumi 的 Harness、权限 authority、Goal/Pursuit 或跨 New UI/TUI 协议语义。

## 许可证与治理

上游采用 MIT License。若未来复制或改写具体组件，必须：

1. 在 CC-01 provenance 清单记录来源 URL、固定 commit、原文件和本地目标；
2. 保留 MIT copyright/permission notice；
3. 做供应链与无障碍审查；
4. 用 Naumi typed fixtures 和跨平台测试重新验收，不能以截图相似作为完成标准。

当前只借鉴公开设计和测试方法，不复制源码，因此不新增运行时依赖或第三方代码。
CC-01.2a 当前提交的 scope 只绑定本地 Claude source，不能作为 Brainless 的 MIT 许可证证据；若未来
复制或改编 Brainless 组件，必须为其单独建立 source identity、license scope 与 provenance。

## 已落实的最小动作

UI-17.2f 已实现 Naumi 自有的 terminal golden-capture prototype：固定尺寸、同一事件 fixture、ANSI/text 两种
输出、New UI/TUI 双 surface、fixture/frame digest 和缺失锚点失败。实现与验收见
[UI-17.2f 双端终端 Golden Capture](../cli-ui/UI-17-2f-terminal-golden-capture.md)。

下一步只把该入口用于 CC-02 current/Ink 同 fixture 对照和 UI-16 跨平台真实 PTY 扩展；本评估仍保持
`reference`，不因 capture 落地而采用 Brainless renderer。

## 来源

- https://github.com/theswerd/brainless
- https://github.com/theswerd/brainless/blob/main/package.json
- https://github.com/theswerd/brainless/commits/main/
- https://github.com/theswerd/brainless/blob/main/LICENSE
