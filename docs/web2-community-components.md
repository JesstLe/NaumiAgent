# Web2 社区组件接入记录

## 2026-09-11：COSS 文件树

- 来源：`cosscom/coss` 的 `apps/origin/registry/default/components/comp-575.tsx` 与 `apps/origin/registry/default/ui/tree.tsx`。
- 许可：COSS 仓库采用混合许可；`apps/origin` 在仓库 `LICENSING.md` 和该目录 README 中明确为 MIT。本项目仅适配这部分，不使用仓库其他 AGPL 目录代码。
- 接入位置：Web2 右侧“文件”页上半区；原有会话附件保留在下半区。
- 底层：新增认证只读接口 `GET /api/v1/workspace/tree`，共享 API client 和类型位于 `frontend/shared`，因此旧 Web 后续也能直接复用，不在 Web2 中重复实现请求逻辑。
- 真实行为：目录树来自当前 daemon 的 `workspace_root`；目录优先、名称排序，支持鼠标与键盘展开、聚焦和选择。
- 安全边界：忽略 `.git`、`.naumi`、虚拟环境、构建产物、缓存和 `node_modules`；忽略符号链接；每个路径再次确认位于工作区内；最多返回 2500 项和 10 层。树保持只读，没有提供会误导用户的拖拽移动。
- 样式：保留参考组件的缩进导引线、无大圆角行、文件类型图标、选中与焦点反馈，使用 Web2 浅色配色。

## 2026-09-11：AI Message 操作条与 Agent Avatar

- 来源：21st.dev `@educalvolpz/ai-message` 与 `@educalvolpz/agent-avatar`，页面公开许可均为 MIT。
- 获取边界：公开页面可读取 Usage 和行为说明，但 `Component.tsx` 的资源接口返回 401；本项目按公开 API 与视觉行为独立适配，没有声称复制不可访问的源码。
- AI Message 不替换 Web2 原有消息气泡、宽度、间距或富文本容器，只在助手消息正文下方增加时间、复制、重新生成、赞和踩操作条。复制调用系统剪贴板；重新生成通过共享控制器再次提交该回答对应的用户请求；赞踩状态按消息保存在本地偏好中。
- Agent Avatar 使用 Canvas 生成 7×7 镜像像素图；seed 决定配色和像素，同一执行保持稳定。头像仅用于执行时间线，执行时轻微呼吸；高 DPI 清晰绘制并支持 reduced motion，不改变原消息布局。

## 2026-09-11：AI Sources

- 来源：21st.dev `@educalvolpz/ai-sources`，公开页面标注 MIT，依赖 `lucide-react` 与 `framer-motion`。
- 获取边界与上两个组件相同：依据公开 Usage 和行为说明独立适配。
- 接入位置：每条助手消息正文下方。优先读取消息 metadata 中真实的 `sources`、`citations` 或 `references`；不存在结构化来源时，仅提取回复正文中已经出现的 HTTP(S) 链接。
- 交互：来源列表折叠、计数、域名、外链与单条摘要原位展开；动效使用 Framer Motion，系统开启 reduced motion 时由浏览器和 CSS 降低动态效果。
- 安全：拒绝非 HTTP(S)、带 URL 用户名或密码的地址；不生成、补写或暗示不存在的来源。

## 2026-09-11：Border Beam

- 21st.dev `@larsen66/border-beam` 指向 `Jakubantalik/Libraries/packages/border-beam`，许可证为 MIT。
- npm 尚未发布 GitHub 当前的 1.4.0（注册表最新为 1.3.0），因此本项目在 `frontend/web2/src/community/border-beam` 保留 1.4.0 GitHub 源码与 LICENSE，不降级安装旧包。仅清理了 5 处上游未使用的函数／参数，以通过本项目更严格的 TypeScript `noUnused` 检查，运行逻辑不变。
- 接入位置：Web2 输入框外层。Agent 执行时启用彩色光束边框，停止后按上游淡出状态收口；采用浅色主题并降低亮度、饱和度和光晕强度，避免破坏 Codex 风格。
- 上游实现包含离屏暂停、元素圆角检测、IntersectionObserver、ResizeObserver、共享限帧动画驱动和 reduced motion 处理；这些能力均保留。

本轮四项组件已经全部接入。

## 2026-09-11：发送失败重试

- 共享工作区控制器会单独记录最近一次真实发送失败的原始消息；主动停止、连接错误、附件错误等其他提示不会被误判为可重试发送。
- Web 与 Web2 的错误提示都使用同一个 `retryFailedSend()` 底层动作。重试期间按钮禁用，成功后自动清除失败状态与错误提示。
- 重试会复用失败尝试中的用户消息，不会在对话中重复插入相同消息；切换会话时清除旧失败状态，避免把上一会话内容发送到新会话。
