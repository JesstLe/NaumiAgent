# Web2 界面

Codex 风格布局的源码和 CSS 位于本目录 `src`，包名 `@naumi/web2`，与 `frontend/web` 同级。

只处理界面展示与布局偏好，使用 `@naumi/shared` 提供的共享 API、会话、流式消息、附件、审批、模型切换与停止操作。

应用统一入口负责加载此界面，访问地址仍为 `/web2`。在本目录执行 `pnpm dev`／`pnpm build` 会调用统一运行入口。
