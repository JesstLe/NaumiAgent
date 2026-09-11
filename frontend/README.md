# Web 前端目录

```text
frontend/
├── web/        # 原版界面、统一应用装配入口、Vite 构建和浏览器集成测试
├── web2/       # Codex 风格界面与 CSS
├── shared/     # 两版界面共同依赖的 API、控制器、状态、平台适配与单元测试
├── terminal-ui/# 独立终端界面，不纳入本 Web workspace
├── package.json
├── pnpm-workspace.yaml
└── pnpm-lock.yaml
```

`web` 与 `web2` 是同级界面模块。共用能力只能在 `shared` 实现，`shared` 不得导入任一界面的源码。`web2` 通过 `@naumi/shared` 调用公共能力，不引用 `web/src`。

统一的应用入口仍在 `web/src/App.tsx`，负责组合两版视图并在路由上方安装同一个 `WorkspaceProvider`。原版访问 `/chat`，新版访问 `/web2`；页面切换继续共享同一套会话、草稿和正在执行的请求。

## 运行与构建

在本目录运行：

```powershell
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1 --port 5174 --strictPort
pnpm build
pnpm test
pnpm e2e --workers=2
```

也可以继续在 `web` 中运行原来的 `npm run dev`、`npm run build` 命令。依赖统一由本目录的 pnpm workspace 和锁文件管理。`web2` 的 `pnpm dev`／`pnpm build` 转发到统一入口，不另起一套控制器。

构建产物保持 `web/dist`，兼容现有 Windows 打包脚本。共享层单元测试放在 `shared/tests`，由统一 Vitest 配置收集；跨界面测试放在 `web/tests/e2e`。
