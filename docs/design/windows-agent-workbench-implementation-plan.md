# Windows NaumiAgent Workbench 实现计划（Web-first 版）

> 生成日期：2026-07-14
> 本计划基于现有 macOS SwiftUI Workbench 的设计文档与 Python 后端 API 合同，规划 Windows 客户端与 Web 前端的实现路径。
> 核心决策：Windows app 与 Web 前端共享同一套 React + TypeScript UI 代码，Windows 端使用 Tauri 2 包装为原生桌面应用。

---

## 1. 前提假设

- **Web 优先**：先实现一个可在浏览器里运行的 Web 前端，用于快速预览和迭代 UI；Windows 桌面端是同一套 UI 的 Tauri 2 包装。
- **Web 技术栈**：React 18 + TypeScript + Vite + Tailwind CSS + react-i18next。
- **状态管理**：Zustand（轻量、易测试、支持跨组件共享）。
- **Windows 包装**：Tauri 2（Rust + WebView2），提供进程管理、文件系统、凭据存储等原生能力。
- **代码位置**：
  - Web UI：`frontend/web/`
  - Windows Tauri 壳：`apps/windows/NaumiAgentWorkbench/`
- **UI 语言**：默认中文（`zh-CN`），保底英文（`en-US`），与 mac app 规格保持一致。
- **MVP 页面**：总览（Dashboard）、对话（Chat）、任务市场（Task Market）、审查（Reviews）、时间线（Timeline）、工作区（Worktrees）、设置（Settings）。
- **Daemon 管理**：Windows app 负责发现 `naumi-agent` / `python -m naumi_agent`，绑定 `127.0.0.1`，在 `8765-8799` 中选可用端口，启动进程、健康检查、收集 stdout/stderr、异常退出提示、退出时询问是否保留 daemon。Web 浏览器开发模式依赖外部已启动 daemon。
- **Token 存储**：Tauri 使用 Windows DPAPI / Credential Locker；浏览器开发模式使用 `localStorage`（明确标注为 dev-only）。
- **设置/状态存储**：Tauri 使用 `%LOCALAPPDATA%\NaumiAgentWorkbench\`；浏览器开发模式使用 `localStorage`。
- **日志**：Tauri 写入 `%LOCALAPPDATA%\NaumiAgentWorkbench\logs\`；浏览器模式写入控制台。
- **后端合同**：完全复用 macOS Workbench 的 REST + WebSocket 合同，不在 UI 里重写业务逻辑。

---

## 2. 目标概述

为 Windows 用户提供原生桌面 Workbench 体验，同时为团队提供一个可实时预览的 Web 前端。

核心目标：

1. **一套 UI 代码同时服务 Web 预览和 Windows 桌面**。
2. 在浏览器里即可实时查看 UI 进展，无需等待桌面打包。
3. Windows 桌面端具备原生能力：daemon 管理、凭据存储、文件系统、进程树终止。
4. 用户可见文案默认中文，英文 fallback。
5. 复用 macOS Workbench API 合同和数据模型，后端保持不变。
6. 先发布 Tauri 打包的 portable `.exe`，后续可选 `.msi` / Microsoft Store。
7. MVP 页面与 macOS 保持一致：Dashboard、Chat、Task Market、Reviews、Timeline、Worktrees、Settings。

---

## 3. 范围

### MVP 内

- Web 前端项目脚手架（Vite + React + TS + Tailwind + i18n）。
- Tauri 2 Windows 项目脚手架。
- 跨平台抽象层：API client、event client、settings storage、token storage、shell launcher。
- Dashboard、Chat、Task Market、Reviews、Timeline、Worktrees、Settings 页面。
- 左侧导航栏 + 右侧上下文面板布局（见 [`windows-agent-workbench-interface-spec.md`](windows-agent-workbench-interface-spec.md)）。
- Windows 本地 daemon 管理（Tauri Rust commands）。
- Web 浏览器开发模式：连接外部已启动 daemon。
- 中文默认 UI，英文 fallback。
- 单元测试（Vitest）和至少一个端到端冒烟测试（Playwright）。

### MVP 外

- 流式 Chat 响应（Chat MVP 仅支持非流式 + 创建 issue）。
- Dashboard Canvas 完整拖拽图布局（MVP 为列表/图占位）。
- Reviews 完整 diff viewer（MVP 仅展示文件树和验证证据）。
- Timeline 回放动画。
- 多 workspace registry UI（超出基础持久化）。
- Cloud sync。
- `.msi` / Microsoft Store 分发（后续阶段）。
- 打包 Python runtime（仍依赖本机 Python 环境）。
- 独立的 Web 部署（MVP 阶段 Web 前端只用于开发和本地 Tauri 包装，不对外发布）。

---

## 4. 推荐技术栈

### Web 前端

| 层级 | 技术 | 理由 |
|------|------|------|
| UI 框架 | React 18 | 生态成熟、组件化、热重载快。 |
| 语言 | TypeScript 5 | 类型安全、可维护、与后端 DTO 对齐方便。 |
| 构建工具 | Vite | 秒级热更新、modern bundle、支持 proxy。 |
| 样式 | Tailwind CSS | 快速实现界面规格中的密度和颜色系统。 |
| 状态 | Zustand | 轻量、无样板、支持持久化和跨组件共享。 |
| 路由 | React Router 6 | 页面切换、URL 状态、懒加载。 |
| i18n | react-i18next | 成熟、支持 zh-CN/en-US fallback。 |
| HTTP | fetch / axios | 轻量、易 mock。 |
| WebSocket | 原生 `WebSocket` | 无额外依赖。 |
| 测试 | Vitest + React Testing Library + Playwright | 单元 + UI 冒烟。 |

### Windows 桌面壳

| 层级 | 技术 | 理由 |
|------|------|------|
| 框架 | Tauri 2 | 用 Web 技术做 UI，Rust 做原生桥，生成小体积 `.exe`。 |
| WebView | WebView2（Windows 11 自带） | 无需额外安装、性能接近 Chrome。 |
| 原生桥 | Rust | 进程管理、文件系统、DPAPI/Credential Locker。 |
| 打包 | Tauri build (`tauri build`) | 输出 `.exe`、installer `.msi`、`.nsis`。 |

### 备选：Electron

如果团队不熟悉 Rust 或需要更复杂的 Node.js 原生集成，可选用 Electron：

- **优势**：Node 生态、进程管理简单、`child_process` 直接可用。
- **劣势**：安装包大、启动慢、内存占用高。
- **结论**：默认推荐 Tauri；若 Rust 学习成本不可接受，再评估 Electron。

---

## 5. 架构

### 5.1 整体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│                     浏览器 / WebView2                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  React UI (shared)                                        │  │
│  │  - pages/ Dashboard, Chat, Task Market, ...               │  │
│  │  - components/ StatusBadge, RiskBadge, LeaseChip, ...     │  │
│  │  - stores/ AppState (Zustand)                             │  │
│  │  - api/ WorkbenchApiClient                                │  │
│  │  - events/ WorkbenchEventClient                           │  │
│  │  - platform/ PlatformAdapter                              │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │ Browser Mode      │ Tauri Mode        │
          │ (dev preview)     │ (Windows app)     │
          │                   │                   │
          │ localStorage      │ Rust commands     │
          │ fetch to 127.0.0.1│ fetch to 127.0.0.1│
          │ manual daemon     │ managed daemon    │
          └───────────────────┴───────────────────┘
```

### 5.2 跨平台抽象层

在 Web UI 中定义统一接口，运行时根据 `window.__TAURI__` 是否存在选择实现：

```text
frontend/web/src/platform/
  PlatformAdapter.ts          # 统一接口
  BrowserPlatformAdapter.ts   # 浏览器实现
  TauriPlatformAdapter.ts     # Tauri 实现
  createPlatformAdapter.ts    # 工厂
```

接口示例：

```ts
interface PlatformAdapter {
  // 存储
  getSetting(key: string): Promise<string | null>;
  setSetting(key: string, value: string): Promise<void>;

  // 凭据
  getToken(): Promise<string | null>;
  setToken(token: string): Promise<void>;
  removeToken(): Promise<void>;

  // Daemon 管理（仅 Tauri）
  startDaemon?(config: DaemonConfig): Promise<DaemonStatus>;
  stopDaemon?(): Promise<void>;
  getDaemonLogs?(): Promise<string[]>;

  // Shell
  openInExplorer?(path: string): Promise<void>;
  openInTerminal?(path: string): Promise<void>;

  // 日志
  log(level: 'info' | 'warn' | 'error', message: string): Promise<void>;
}
```

### 5.3 项目结构

```text
frontend/web/
  package.json
  vite.config.ts
  tsconfig.json
  tailwind.config.js
  index.html
  src/
    main.tsx
    App.tsx
    routes.tsx
    i18n/
      index.ts
      zh-CN.json
      en-US.json
    api/
      WorkbenchApiClient.ts
      ApiException.ts
      routeTemplates.ts
      types/
        snapshot.ts
        issue.ts
        task.ts
        event.ts
        daemon.ts
        capabilities.ts
        ...
    events/
      WorkbenchEventClient.ts
      useEventStream.ts
    stores/
      appStore.ts
      connectionStore.ts
      localeStore.ts
    platform/
      PlatformAdapter.ts
      BrowserPlatformAdapter.ts
      TauriPlatformAdapter.ts
      createPlatformAdapter.ts
    components/
      Shell/
        MainLayout.tsx
        LeftNavigation.tsx
        RightPanel.tsx
        TopMenuBar.tsx
      Chat/
        ChatPage.tsx
        MessageList.tsx
        Composer.tsx
        IssueDraftPanel.tsx
      Dashboard/
        DashboardPage.tsx
        StatusStrip.tsx
        IssueQueue.tsx
        AuditTimeline.tsx
      TaskMarket/
        TaskMarketPage.tsx
        IssueTable.tsx
        ActiveLeaseStrip.tsx
      Reviews/
        ReviewsPage.tsx
        ApprovalInbox.tsx
        ValidationRunList.tsx
      Timeline/
        TimelinePage.tsx
        EventList.tsx
      Worktrees/
        WorktreesPage.tsx
        WorktreeTable.tsx
      Settings/
        SettingsPage.tsx
      common/
        StatusBadge.tsx
        RiskBadge.tsx
        LeaseChip.tsx
        ContextHealthIndicator.tsx
        FailureCard.tsx
        ApprovalPanel.tsx
    hooks/
      useWorkbenchApi.ts
      useSnapshot.ts
      useCapabilities.ts
    utils/
      formatDate.ts
      redactSecrets.ts
      portScanner.ts          # TCP port scan (browser can't, Tauri can)
  tests/
    unit/
    e2e/

apps/windows/NaumiAgentWorkbench/
  package.json
  src-tauri/
    Cargo.toml
    tauri.conf.json
    src/
      main.rs
      lib.rs
      daemon.rs             # start/stop/health/log
      storage.rs            # settings/log paths
      credentials.rs        # DPAPI / Credential Locker
      shell.rs              # open explorer / git bash
      logging.rs            # log redaction/rotation
    capabilities/
      default.json
  src/                       # Tauri 项目入口（引用 frontend/web 构建产物）
  scripts/
    dev.ps1
    build.ps1
```

### 5.4 Tauri 原生能力（Rust commands）

| 能力 | Rust 模块 | 说明 |
|------|----------|------|
| Daemon 发现与启动 | `daemon.rs` | 解析 `naumi-agent` / `python -m naumi_agent`，选端口，启动进程，捕获 stdout/stderr。 |
| Daemon 健康检查 | `daemon.rs` | 轮询 `GET /workbench/daemon/status`。 |
| Daemon 日志 | `logging.rs` | 写 `%LOCALAPPDATA%\NaumiAgentWorkbench\logs\daemon.log`，按日轮转、保留 7 天。 |
| 凭据存储 | `credentials.rs` | Windows DPAPI 加密 token 到 `%LOCALAPPDATA%\NaumiAgentWorkbench\token.bin`；或尝试 Credential Locker。 |
| 设置持久化 | `storage.rs` | 读写 `settings.json`、`daemon-launch.json`、`workspace-registry.json`。 |
| Shell 启动 | `shell.rs` | 打开资源管理器、Git Bash（遵循 `shell.py` 规则）。 |
| 进程树终止 | `daemon.rs` | `TerminateProcess` / `taskkill /T /F`。 |

### 5.5 数据流

```text
App 启动
  -> 加载 platform adapter
  -> 恢复 locale
  -> 从 adapter 加载 token
  -> 构建 API/event clients
  -> 若 Tauri 且启用自动启动：
       invoke('start_daemon')
         -> 解析 naumi-agent / python -m naumi_agent
         -> 选端口 8765-8799
         -> Rust 启动进程，日志写入文件
         -> 健康检查
         -> 返回 daemon status
  -> ConnectionCoordinator.refresh()
       -> GET /workbench/daemon/status
       -> GET /workbench/capabilities
       -> GET /workbench/bootstrap
       -> GET /workbench/sessions/{id}/snapshot
       -> 连接 WebSocket 事件流
       -> 预热列表
  -> React components 订阅 Zustand store 变化
```

### 5.6 浏览器开发模式

- 运行 `npm run dev` 启动 Vite。
- 页面检测 `window.__TAURI__` 不存在，使用 `BrowserPlatformAdapter`。
- Token/settings 存在 `localStorage`；页面顶部显示“浏览器模式：daemon 需手动启动”提示。
- 开发者先手动运行 `naumi serve` 或 `naumi-agent api`，Web UI 连接 `http://127.0.0.1:8765`。
- Vite 配置 `server.proxy` 代理 `/api` 和 `/ws` 以绕过 CORS（若后端未配置跨域）。

### 5.7 国际化

- 使用 `react-i18next`，资源文件 `zh-CN.json` / `en-US.json`。
- 默认 `zh-CN`，fallback `en-US`，最终 fallback 为 key 名。
- 用户输入、事件 type、枚举值、agent id、worktree name 不翻译。

### 5.8 错误处理

- `ApiException` 类型：覆盖 mac app 中定义的错误码。
- 全局错误 banner（Zustand `appStore.lastError`）。
- 错误文案走 i18n，不暴露 token。

---

## 6. macOS 模块到 Web/Tauri 的映射

| macOS 文件 | Web/Tauri 等价物 | 说明 |
|------------|------------------|------|
| `NaumiAgentWorkbenchApp.swift` | `frontend/web/src/main.tsx` + `App.tsx` | 应用入口。 |
| `AppState.swift` | `frontend/web/src/stores/appStore.ts` | Zustand 全局状态。 |
| `AppEnvironment.swift` | `frontend/web/src/platform/createPlatformAdapter.ts` | 平台能力注入。 |
| `AppRoute.swift` | `frontend/web/src/routes.tsx` | React Router 路由。 |
| `WorkbenchAPIClient.swift` | `frontend/web/src/api/WorkbenchApiClient.ts` | fetch REST 客户端。 |
| `WorkbenchEventClient.swift` | `frontend/web/src/events/WorkbenchEventClient.ts` | WebSocket 客户端。 |
| `APIError.swift` | `frontend/web/src/api/ApiException.ts` | 类型化错误。 |
| `DaemonController.swift` | `frontend/web/src/stores/connectionStore.ts` | 连接 + 事件流编排。 |
| `DaemonProcessController.swift` | `apps/windows/.../src-tauri/src/daemon.rs` | Rust daemon 管理。 |
| `DaemonLaunchConfiguration.swift` | `apps/windows/.../src-tauri/src/daemon.rs` 中的配置 | 端口、可执行文件路径。 |
| `LocalAuthTokenStore.swift` | `apps/windows/.../src-tauri/src/credentials.rs` | Windows 凭据存储。 |
| `Localization/AppStrings.swift` | `frontend/web/src/i18n/zh-CN.json`、`en-US.json` | 本地化字符串。 |
| `Features/Dashboard/*` | `frontend/web/src/components/Dashboard/*` | Dashboard。 |
| `Features/Chat/*` | `frontend/web/src/components/Chat/*` | Chat。 |
| `Features/TaskMarket/*` | `frontend/web/src/components/TaskMarket/*` | 任务市场。 |
| `Features/Reviews/*` | `frontend/web/src/components/Reviews/*` | 审查。 |
| `Features/Timeline/*` | `frontend/web/src/components/Timeline/*` | 时间线。 |
| `Features/Worktrees/*` | `frontend/web/src/components/Worktrees/*` | 工作区。 |
| `Features/Settings/*` | `frontend/web/src/components/Settings/*` | 设置。 |
| `Components/*` | `frontend/web/src/components/common/*` | 可复用组件。 |

---

## 7. 实现阶段

### Phase 1：Web 前端与 Tauri 脚手架

**交付物**：

- `frontend/web/`：Vite + React + TS + Tailwind + react-i18next 项目。
- `apps/windows/NaumiAgentWorkbench/`：Tauri 2 项目，能加载 Web UI。
- `vite.config.ts` 配置代理 `/api`、`/ws`。
- `package.json` scripts：`dev`、`build`、`test`、`tauri dev`、`tauri build`。
- 基础 `MainLayout`（空三栏布局）。

**验证**：

- `npm run dev` 后浏览器能打开空白三栏页面，标签为中文。
- `npm run tauri dev` 后 Windows 桌面窗口能打开同一页面。

### Phase 2：跨平台抽象层与基础设施

**交付物**：

- `PlatformAdapter` 接口。
- `BrowserPlatformAdapter`（localStorage token/settings，console log）。
- `TauriPlatformAdapter`（invoke Rust commands）。
- `createPlatformAdapter` 工厂。
- `appStore`（Zustand）：session、snapshot、connection、locale、lastError。
- `localeStore`：语言切换与持久化。

**验证**：

- 单元测试：adapter 工厂选择正确、settings 读写往返、locale fallback。
- Tauri 侧 Rust 单元测试：settings 路径正确。

### Phase 3：API 合同层

**交付物**：

- TypeScript DTO 类型（覆盖 backend 所有模型）。
- `WorkbenchApiClient`：所有 REST 方法 + bearer token 注入 + 路由模板展开。
- `ApiException` 映射。
- Vitest mock fetch 测试。

**验证**：

- 所有 GET/POST endpoint 都有 mock 测试。
- JSON 反序列化测试使用真实后端响应片段。

### Phase 4：Tauri 原生能力

**交付物**：

- Rust `daemon.rs`：start/stop/health/log。
- Rust `storage.rs`：settings/log 路径。
- Rust `credentials.rs`：token 存储（DPAPI 优先，Credential Locker 备选）。
- Rust `shell.rs`：open explorer / git bash。
- Rust `logging.rs`：日志轮转与脱敏。

**验证**：

- Rust 单元测试：路径生成、日志轮转、token 加解密。
- 手动测试：Tauri app 启动/停止真实 `naumi serve`，UI 显示 PID、端口、日志。
- 崩溃测试：外部 kill daemon，UI 报告异常退出。

### Phase 5：连接与事件流

**交付物**：

- `WorkbenchEventClient`（WebSocket）。
- `connectionStore`：bootstrap、capabilities、snapshot、重连、session 切换。
- 全局 loading / error banner。

**验证**：

- 连接真实后端，snapshot 和列表加载。
- WebSocket 断线重连。
- session 切换清除 session 级状态。

### Phase 6：Dashboard 页

**交付物**：

- `DashboardPage` + 子组件。
- Status strip、mission tree、issue queue、audit timeline、inspector 占位。

**验证**：

- 浏览器中展示真实 snapshot。
- 选择 issue 更新右侧面板 inspector。

### Phase 7：Chat 页

**交付物**：

- `ChatPage`、MessageList、Composer、IssueDraftPanel。
- 非流式发送消息。
- “同时创建任务”流程。

**验证**：

- 浏览器中发送消息并看到回复。
- 创建关联 issue 后在 Task Market 可见。

### Phase 8：Task Market 页

**交付物**：

- `TaskMarketPage`、IssueTable、filters、ActiveLeaseStrip。
- claim / release lease。
- 内联创建 issue。

**验证**：

- claim 产生 lease；release 后 lease 消失。

### Phase 9：Reviews 页

**交付物**：

- `ReviewsPage`、ApprovalInbox、ValidationRunList。
- approve/reject、run validation。

**验证**：

- 运行验证、审批状态变化。

### Phase 10：Timeline 页

**交付物**：

- `TimelinePage`、EventList、filters、raw audit log。

**验证**：

- 创建 mission 后事件加载、筛选生效。

### Phase 11：Worktrees 页

**交付物**：

- `WorktreesPage`、WorktreeTable。
- keep/remove（含二次确认）。
- Tauri 端 “Open in Explorer”、“Open in Terminal”。

**验证**：

- keep/remove 状态变化。
- Tauri 打开资源管理器和 Git Bash。

### Phase 12：Settings 页

**交付物**：

- `SettingsPage`。
- 语言切换、daemon 设置、治理策略（intent locks/decisions）。

**验证**：

- 切换语言所有标签更新。
- 设置持久化。

### Phase 13：打包与分发

**交付物**：

- `scripts/windows/build-web.ps1`：构建 web。
- `scripts/windows/build-tauri.ps1`：构建 Tauri `.exe`。
- `scripts/windows/dev.ps1`：一键启动 Vite + Tauri dev。
- `README.md`：Windows 安装与开发说明。
- 输出到 `artifacts/`。

**验证**：

- Tauri `.exe` 在干净 Windows 机器上运行。
- 日志和设置写入 `%LOCALAPPDATA%`。

### Phase 14：质量与回归

**交付物**：

- Vitest 单元测试（API client、route expander、stores、adapter）。
- Playwright 端到端冒烟测试（至少覆盖导航 + Chat + Task Market）。
- 后端回归：`ruff check src/` 和 `pytest tests/ -x`（如有后端改动）。

**验证**：

- 所有测试通过。
- 手动端到端冒烟：启动 app、启动 daemon、创建 mission、聊天、创建 issue、claim issue、运行验证、审查、切换语言、退出。

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Tauri Rust 学习成本 | 中 |  daemon / storage / credentials 逻辑相对集中；必要时可换 Electron。 |
| 浏览器模式与 Tauri 模式行为不一致 | 中 | 抽象 `PlatformAdapter`，统一接口；两种模式都有单元测试。 |
| 后端 CORS 未配置，浏览器开发模式连不上 | 中 | Vite dev server 代理 `/api` 和 `/ws`；或后端增加 CORS allow `http://localhost:5173`。 |
| WebSocket 在 Tauri WebView2 下重连异常 | 中 | 使用原生 `WebSocket` 并自行实现重连策略；在不同网络条件下测试。 |
| Windows 凭据存储在 Tauri 中实现复杂 | 中 | 先用 DPAPI 加密文件 `token.bin`，稳定后再迁移 Credential Locker。 |
| 进程树终止残留 Python 子进程 | 高 | Rust 使用 Windows API `TerminateJobObject` 或 `taskkill /T /F`。 |
| Git Bash 未安装 | 中 | 显示本地化错误，引导安装或设置 `NAUMI_GIT_BASH`。 |
| Web UI 与 mac app 视觉差异过大 | 低 | 严格遵循 [`windows-agent-workbench-interface-spec.md`](windows-agent-workbench-interface-spec.md) 的颜色、密度、组件规格。 |
| 本地化 key 缺失 | 低 | 加测试确保每个 key 在 `zh-CN` 中存在。 |

---

## 9. MVP 完成定义

- Web 前端 `frontend/web/` 能独立在浏览器运行，展示完整三栏布局和所有 MVP 页面。
- Tauri Windows app 能构建为 portable `.exe` 并在 Windows 11 上运行。
- App 能启动、管理、停止本地 `naumi-agent` / `python -m naumi_agent` daemon（`127.0.0.1:8765-8799`）。
- Dashboard、Chat、Task Market、Reviews、Timeline、Worktrees、Settings 页面可用。
- Chat 支持非流式消息和创建关联 issue。
- 设置和状态在 Tauri 下持久化到 `%LOCALAPPDATA%\NaumiAgentWorkbench\`。
- Daemon 日志写入 `%LOCALAPPDATA%\NaumiAgentWorkbench\logs\`。
- Bearer token 在 Tauri 下使用 DPAPI/Credential Locker 安全存储。
- UI 默认中文（`zh-CN`），英文（`en-US`）fallback。
- 代码注释用英文，用户文案用中文。
- 没有 prompt 套壳或假实现，每个功能都有真实代码逻辑。
- 后端改动通过 `ruff check src/` 和 `pytest tests/ -x`。
- Vitest 和 Playwright 测试通过。

---

## 10. 建议后续细化文档

- `docs/design/windows-agent-workbench-architecture.md`
- `docs/design/windows-agent-workbench-local-daemon-bridge.md`
- `docs/design/windows-agent-workbench-packaging-distribution.md`
- `docs/design/windows-agent-workbench-web-frontend.md`（Web 前端专属细节，可选）

### 本次计划最关键的实现文件

- `frontend/web/src/api/WorkbenchApiClient.ts`
- `frontend/web/src/events/WorkbenchEventClient.ts`
- `frontend/web/src/stores/appStore.ts`
- `frontend/web/src/platform/TauriPlatformAdapter.ts`
- `apps/windows/NaumiAgentWorkbench/src-tauri/src/daemon.rs`
- `apps/windows/NaumiAgentWorkbench/src-tauri/src/credentials.rs`
- `apps/windows/NaumiAgentWorkbench/src-tauri/tauri.conf.json`
