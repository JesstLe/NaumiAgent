# NaumiAgent Mac Agent Workbench Product Architecture Roadmap

> 本文定义 Mac Agent Workbench 从轻量 MVP 到完整产品的架构演进路线。  
> 目标：第一版可以轻量，但所有早期决策都必须能平滑走向完整产品。

## 1. 核心判断

NaumiAgent Mac Agent Workbench 的最终形态不是“一个 SwiftUI 客户端连接用户手动启动的 Python 服务”，而是一个完整的本地 Agent OS：

```text
SwiftUI 原生 Mac App
  -> NaumiAgent Local Daemon
    -> Workbench Kernel
      -> Agent Runtime
      -> Task Market
      -> Git Worktree Manager
      -> Validation Runner
      -> Decision Log
      -> Audit Log
      -> Local Memory
  -> optional Cloud Sync / Team Collaboration
```

但是 MVP 不应该一开始承担完整产品的全部复杂度。MVP 应该先证明核心体验：

```text
Mission
  -> Issue
  -> Agent Claim / Lease
  -> Worktree
  -> Validation
  -> Failure / Approval
  -> Dashboard Snapshot
```

因此本项目采用双线设计：

```text
MVP line: 最快验证本地协作内核和原生壳体验
North Star line: 最终可打包、可升级、可治理、可扩展的完整 Mac 产品
```

## 2. 不变原则

这些原则从 MVP 第一天就必须成立。

### 2.1 SwiftUI 是产品壳，不重写 Agent Runtime

SwiftUI 负责：

```text
窗口
导航
Dashboard
Task Market
Reviews
Timeline
Settings
本地通知
Keychain
workspace 授权体验
```

Python 负责：

```text
AgentEngine
工具系统
任务系统
worktree 管理
验证命令
审计事件
记忆系统
模型调用
```

禁止路线：

```text
用 Swift 重写 Agent runtime
用 Swift 直接读写 Workbench SQLite 业务表
让 SwiftUI 绕过 Workbench API 操作任务状态
```

### 2.2 UI 永远通过稳定 API / Event Contract 访问内核

当前 MVP 可以使用：

```text
127.0.0.1 FastAPI + WebSocket
```

未来完整产品可以切换为：

```text
App-managed daemon
embedded local service
launch agent
private localhost bridge
```

但 SwiftUI 层不应该感知底层运行方式变化。

稳定边界：

```text
GET /workbench/sessions/{session_id}/snapshot
workbench/snapshot
workbench/event
```

### 2.3 Workbench Kernel 是产品核心，不绑定某一种 UI

Workbench Kernel 必须同时服务：

```text
SwiftUI Mac App
terminal-ui
API clients
future web dashboard
future cloud sync
```

因此：

- Dashboard 状态以后端 snapshot 为准。
- UI 不自行推断最终业务状态。
- 事件协议必须向后兼容。
- 所有写操作必须进入 AuditEvent。

### 2.4 Local-first 是基本盘，Cloud 是增强层

完整产品也必须保证本地可用：

```text
workspace
worktree
validation
audit log
decision log
mission state
```

没有云服务时仍可运行。

云只负责增强：

```text
team sharing
cross-device sync
remote PR status
backup
collaborative review
```

### 2.5 危险动作从 MVP 起按最终产品标准设计

以下动作即使 MVP 阶段也不能随便放开：

```text
force remove worktree
execute shell command
approve high risk
auto merge
delete mission data
modify permission policy
change model/provider credentials
```

这些动作必须有：

```text
权限检查
用户确认
审计事件
失败路径
可恢复策略
```

## 3. 架构阶段

### Phase 0: Documentation and Product Shape

状态：已完成基础版。

产物：

```text
PRD
User Flows
Architecture
Domain Model
Event Protocol
Governance
Interface Spec
Test Strategy
Acceptance Criteria
MVP Implementation Plan
```

目标：

- 产品边界清楚。
- MVP 不做什么清楚。
- 完整产品方向清楚。
- 图形界面方向清楚。

### Phase 1: Workbench Kernel MVP

目标：

```text
实现 Python 侧协作内核。
```

范围：

```text
Mission
IssueMetadata
AgentProfile
Lease
IntentLock
Decision
ValidationRun
FailureCard
AuditEvent
ContextSnapshot
Dashboard Snapshot
```

运行方式：

```text
开发者本地启动 NaumiAgent API
SwiftUI 暂不必须存在
terminal-ui / API test 可验证 snapshot/event
```

为什么先做：

- 先把产品事实层做稳。
- 避免 UI 先行导致假状态。
- 给 SwiftUI shell 提供稳定数据合同。

### Phase 2: SwiftUI Shell MVP

目标：

```text
原生 Mac App 连接本地 Workbench API，展示核心页面。
```

范围：

```text
DashboardView
TaskMarketView
ReviewsView
TimelineView
WorktreesView basic
SettingsView basic
Localization zh-CN / en-US
WorkbenchAPIClient
WorkbenchEventClient
```

运行方式：

```text
用户或开发环境先启动 NaumiAgent API
SwiftUI App 连接 127.0.0.1
断线后自动重连并重新拉 snapshot
```

MVP 允许：

- 后端手动启动。
- 配置写在开发设置里。
- 不打包 Python runtime。

MVP 不允许：

- SwiftUI 直接读写 SQLite。
- SwiftUI 直接运行 git/pytest 业务命令。
- SwiftUI 绕过 API 修改 lease/approval。

### Phase 3: Daemon Manager

目标：

```text
SwiftUI App 能启动、停止、监控 NaumiAgent Local Daemon。
```

能力：

```text
检查 naumi-agent binary
启动本地 daemon
选择端口
健康检查
日志查看
异常重启提示
退出时保留/停止 daemon
```

daemon 约束：

```text
只绑定 127.0.0.1
使用本地 auth token
token 存 Keychain
日志不记录 secret
```

这一阶段仍然不要求把 Python runtime 完整塞进 app bundle。

### Phase 4: Product-grade Local App

目标：

```text
形成可长期本地使用的完整 Mac 产品。
```

能力：

```text
workspace 授权
多 workspace 管理
Keychain credential storage
macOS notifications
menu bar status item
launch at login 可选
crash recovery
settings migration
daemon log rotation
i18n 完整覆盖
```

安全要求：

- 每个 workspace 必须用户显式添加。
- 访问新 repo 必须授权。
- 危险动作必须二次确认。
- high/critical risk 审批不能默认确认。

### Phase 5: Distribution and Updates

目标：

```text
可分发、可更新、可签名的 Mac App。
```

能力：

```text
code signing
notarization
GitHub Releases 或自托管下载
Sparkle auto update
版本迁移
bundle daemon 或 installer-managed daemon
```

此阶段需要决定：

```text
是否内嵌 Python runtime
是否使用 standalone daemon package
是否支持 App Store
是否支持企业内部分发
```

建议：

- 不优先 App Store。
- 先走签名 + notarized direct distribution。
- 自动更新用 Sparkle。

### Phase 6: Cloud and Team Layer

目标：

```text
在不破坏 local-first 的前提下增加团队能力。
```

能力：

```text
cloud backup
team mission sharing
remote review
GitHub PR status sync
cross-device timeline
policy templates
agent reliability analytics
```

约束：

- 本地数据仍是第一事实源。
- 云同步不能阻塞本地执行。
- secret 不进入云端 audit payload。

## 4. MVP 架构和完整产品架构的差异

| 维度 | MVP | 完整产品 |
|------|-----|----------|
| UI | SwiftUI shell 或 terminal-ui 验证 | SwiftUI 原生完整 App |
| 后端启动 | 手动或简单启动脚本 | App 管理 Local Daemon |
| 后端打包 | 依赖本机 Python 环境 | bundled runtime 或 managed daemon |
| 通信 | localhost FastAPI + WebSocket | 稳定本地 bridge，可替换实现 |
| 认证 | 开发 token 或本地配置 | Keychain token + workspace authorization |
| 更新 | git pull / 本地运行 | signed release + auto update |
| 云 | 无 | 可选同步和团队协作 |
| 数据源 | 本地 SQLite | 本地 SQLite + 可选云镜像 |

## 5. 早期必须预留的接口

### 5.1 Daemon Status

即使 Phase 1 不实现 daemon manager，API 也应预留：

```text
GET /api/v1/workbench/daemon/status
```

返回：

```json
{
  "status": "running",
  "version": "0.1.0",
  "pid": 12345,
  "workspace_count": 1,
  "started_at": "2026-06-27T10:00:00"
}
```

### 5.2 Workspace Registry

预留 workspace 概念：

```text
workspace_id
name
path
authorization_state
last_opened_at
default_session_id
```

即使 MVP 只支持一个 repo，也不要把 session 和 repo path 写死。

### 5.3 Event Contract Version

所有 snapshot/event 必须带 version 或能从 protocol contract 推断版本。

```json
{
  "version": 1,
  "type": "workbench/snapshot",
  "payload": {}
}
```

### 5.4 Capabilities Endpoint

SwiftUI 不应该硬猜后端支持什么。

```text
GET /api/v1/workbench/capabilities
```

示例：

```json
{
  "supports_daemon_management": false,
  "supports_workspace_registry": false,
  "supports_validation_runner": true,
  "supports_cloud_sync": false,
  "supported_locales": ["zh-CN", "en-US"]
}
```

### 5.5 Localization Boundary

后端返回：

```text
event type
enum value
raw message
structured code
```

前端负责：

```text
navigation label
button label
status label
empty state
confirmation copy
```

用户可见错误可以由后端提供中文默认，但必须保留 machine-readable code。

## 6. SwiftUI 与 Daemon 的长期关系

### 6.1 MVP 关系

```text
SwiftUI App
  -> http://127.0.0.1:<port>
  -> user/dev started NaumiAgent server
```

优点：

- 简单。
- 可调试。
- 不处理打包复杂度。

缺点：

- 用户需要知道后端是否启动。
- 产品完整度不够。

### 6.2 Phase 3 关系

```text
SwiftUI App
  -> DaemonManager
    -> launch naumi-agent server
    -> health check
    -> log stream
  -> WorkbenchAPIClient
```

这是推荐的中期形态。

### 6.3 完整产品关系

```text
SwiftUI App Bundle
  ├── App UI
  ├── Daemon Manager
  ├── bundled or managed NaumiAgent runtime
  ├── Keychain token
  └── Workspace registry
```

完整产品可以选择两种 runtime 分发方式：

| 方式 | 优点 | 风险 |
|------|------|------|
| bundled Python runtime | 用户无感安装 | 体积大、签名复杂、依赖打包难 |
| managed daemon package | runtime 独立升级 | 安装流程复杂 |

建议先走 managed local binary，成熟后再评估 bundled runtime。

## 7. 安全演进路线

### Phase 1

```text
127.0.0.1 only
developer token
dangerous tools still governed by Python safety layer
```

### Phase 2

```text
SwiftUI confirmation sheets
workspace path display
danger actions disabled without backend permission
```

### Phase 3

```text
Keychain token
daemon auth
workspace authorization
log redaction
```

### Phase 4+

```text
signed daemon
notarized app
policy templates
team governance
cloud sync permission boundary
```

## 8. 数据演进路线

### MVP

```text
SQLite local state
session scoped mission/task/issue
single workspace assumption allowed
```

### Product-grade

```text
workspace registry
schema migrations
backup/restore
log rotation
audit export
```

### Cloud-ready

```text
sync metadata
conflict resolution
team member identity
remote review state
```

## 9. UI 演进路线

### MVP UI

```text
Dashboard
Task Market
Reviews basic
Timeline basic
Settings language
```

### Product-grade UI

```text
Worktrees manager
Daemon health
Workspace switcher
Advanced governance settings
Notification center
Menu bar status
Keyboard shortcuts
```

### Team UI

```text
Shared missions
Remote approvals
Agent reliability analytics
Policy templates
Cross-device replay
```

## 10. 决策清单

### 已定

```text
SwiftUI native Mac shell
Python remains Agent runtime
local-first as base
Chinese default, English fallback
Workbench snapshot/event as UI truth
MVP does not implement autonomous merge
```

### 下一步必须定

```text
Daemon MVP mode: manual start or app-managed light daemon
SwiftUI project location in repo
API auth token format
Workspace authorization data model
SwiftUI i18n file structure
```

### 可以后置

```text
bundle Python runtime
Sparkle update
notarization
cloud sync
team collaboration
App Store distribution
```

## 11. 推荐下一步

接下来补两份更具体的文档：

1. SwiftUI Shell Architecture

```text
docs/design/mac-agent-workbench-swiftui-shell-architecture.md
```

内容：

```text
SwiftUI module structure
AppState
Routing
ViewModels
API client
WebSocket client
i18n
error handling
preview fixtures
```

2. Local Daemon Bridge Design

```text
docs/design/mac-agent-workbench-local-daemon-bridge.md
```

内容：

```text
manual server mode
app-managed daemon mode
health check
port selection
auth token
Keychain
logs
shutdown behavior
future packaging
```

这两份完成后，完整 Mac 产品化开发的准备度会从“方向明确”提升到“可以拆 SwiftUI 实现计划”。
