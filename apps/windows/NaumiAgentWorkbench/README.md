# NaumiAgent Workbench (Windows)

Windows 桌面端 NaumiAgent Workbench，基于 Tauri 2 + React 19 + TypeScript 构建。
Web 前端代码与浏览器开发预览共享 `frontend/web/`，Windows 桌面端是同一套 UI 的 Tauri 包装。

## 前置要求

- Windows 10/11（x64）
- [Node.js](https://nodejs.org/) 20+
- [pnpm](https://pnpm.io/)
- [Rust](https://rustup.rs/)（stable toolchain）
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)（Windows 11 自带）
- [Git for Windows](https://git-scm.com/download/win)（提供 Git Bash，用于"在终端中打开"功能）

## 快速开始

### 一键初始化环境

```powershell
# 在仓库根目录运行
powershell -ExecutionPolicy Bypass -File scripts/windows/setup.ps1
```

该脚本会检查 Python/uv/Node/Git Bash，同步开发环境，安装 naumiagent 命令，并验证配置。

### 开发模式

```powershell
# 启动 Vite dev server + Tauri 桌面窗口
powershell -ExecutionPolicy Bypass -File scripts/windows/dev.ps1
```

或手动启动：

```powershell
cd apps/windows/NaumiAgentWorkbench
cargo tauri dev
```

`tauri.conf.json` 的 `beforeDevCommand` 会自动在 `frontend/web` 运行 `pnpm dev`。

### 浏览器开发预览

无需 Tauri，直接在浏览器中预览 UI：

```powershell
cd frontend/web
pnpm install
pnpm dev
```

打开 `http://localhost:5173`。Vite 会把 `/api` 和 `/ws` 代理到 `127.0.0.1:8765`，
因此需要先手动启动后端：`uv run naumi serve`。

> 浏览器模式下，daemon 管理和"在资源管理器/终端中打开"功能不可用（仅 Tauri 原生环境支持）。

## 构建与打包

### 构建 Web 前端

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows/build-web.ps1
```

### 构建 Tauri 桌面应用

```powershell
# 包含 Web 构建
powershell -ExecutionPolicy Bypass -File scripts/windows/build-tauri.ps1

# 跳过 Web 构建（已构建过）
powershell -ExecutionPolicy Bypass -File scripts/windows/build-tauri.ps1 -SkipWebBuild
```

构建产物：
- 可执行文件：`apps/windows/NaumiAgentWorkbench/src-tauri/target/release/NaumiAgentWorkbench.exe`
- 安装包：`apps/windows/NaumiAgentWorkbench/src-tauri/target/release/bundle/`（`.msi` / `.exe` installer）

## 项目结构

```
frontend/web/                      # 共享 Web 前端（React + TS + Vite + Tailwind）
  src/
    api/                           # WorkbenchApiClient + DTO + 路由模板
    components/                    # 页面组件（Chat, Dashboard, TaskMarket, ...）
    platform/                      # 平台适配层（Browser / Tauri）
    stores/                        # Zustand 状态管理
    i18n/                          # zh-CN / en-US 国际化

apps/windows/NaumiAgentWorkbench/  # Tauri 桌面壳
  src-tauri/
    src/
      lib.rs                       # 应用入口，注册所有 Tauri commands
      daemon.rs                    # 守护进程生命周期管理
      storage.rs                   # 应用数据目录 + JSON 持久化
      secure_storage.rs            # API token 安全存储（Windows Credential Manager）
      shell.rs                     # 打开资源管理器 / Git Bash
      logging.rs                   # 日志轮转 + 机密脱敏
    capabilities/default.json      # 权限白名单
    tauri.conf.json                # Tauri 配置

scripts/windows/
  setup.ps1                        # 环境初始化
  dev.ps1                          # 开发模式
  build-web.ps1                    # 构建 Web 前端
  build-tauri.ps1                  # 构建 Tauri 桌面应用
```

## 原生能力

| 能力 | Tauri 命令 | 说明 |
|------|-----------|------|
| API token 存储 | `get_token` / `set_token` / `remove_token` | Windows Credential Manager |
| 设置持久化 | `get_setting` / `set_setting` | `%LOCALAPPDATA%\NaumiAgentWorkbench\data\settings.json` |
| 守护进程管理 | `start_daemon` / `stop_daemon` / `get_daemon_status` / `get_daemon_logs` | 端口 8765-8799 自动探测，naumi→python 回退 |
| 打开资源管理器 | `open_in_explorer` | 调用 `explorer.exe` |
| 打开终端 | `open_in_terminal` | Git Bash（遵循 NAUMI_GIT_BASH 优先规则） |
| 应用日志 | `write_app_log` | 按日轮转，保留 7 天，自动脱敏 |

应用数据目录：`%LOCALAPPDATA%\NaumiAgentWorkbench\`
- `data/settings.json` — 用户设置
- `data/daemon-launch.json` — 守护进程启动配置
- `logs/app-YYYY-MM-DD.log` — 应用日志
- `logs/daemon.log` — 守护进程输出

## 页面

| 页面 | 功能 |
|------|------|
| 总览 (Dashboard) | 活跃 Agent、待处理 Issue、待审批、验证失败等指标 |
| 对话 (Chat) | 与 Agent 对话，支持"同时创建任务" |
| 任务市场 (Task Market) | 认领/释放 Issue，内联创建 Issue |
| 工作区 (Worktrees) | 保留/删除工作区，打开资源管理器/终端 |
| 审查 (Reviews) | 审批通过/驳回，运行验证 |
| 时间线 (Timeline) | 事件列表，按类型/严重程度筛选 |
| 设置 (Settings) | 语言切换、API 令牌、守护进程配置 |

## 测试

```powershell
# 前端单元测试
cd frontend/web
pnpm test:run

# Rust 单元测试
cd apps/windows/NaumiAgentWorkbench/src-tauri
cargo test --lib

# Rust 集成测试（需要真实 naumi/python 环境）
cargo test -- --ignored
```
