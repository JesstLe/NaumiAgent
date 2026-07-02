# NaumiAgent Mac Agent Workbench SwiftUI Shell Architecture

> 本文定案 SwiftUI 原生 Mac App 的工程结构。  
> 决策：SwiftUI 只做产品壳和本地系统体验，不重写 Python Agent Runtime。

## 1. 结论

Mac App 使用 SwiftUI 原生实现，工程目录放在：

```text
apps/macos/NaumiAgentWorkbench/
```

SwiftUI App 通过稳定的本地 API 和事件协议访问 NaumiAgent Workbench Kernel：

```text
SwiftUI App
  -> WorkbenchAPIClient
  -> http://127.0.0.1:<port>/api/v1
  -> NaumiAgent Python backend
```

现在可以先连接开发者启动的本地服务；未来由 App 管理 daemon。SwiftUI 层不感知底层是手动服务、App-managed daemon，还是打包 runtime。

## 2. 不做的事

SwiftUI 不做：

```text
Agent 推理
工具执行
直接读写 SQLite
直接运行 git / pytest
直接修改 TaskStore
直接创建或删除 worktree
直接审批高风险动作
```

SwiftUI 所有业务写操作必须走 Workbench API。

## 3. SwiftUI 模块结构

推荐结构：

```text
apps/macos/NaumiAgentWorkbench/
  NaumiAgentWorkbench.xcodeproj
  NaumiAgentWorkbench/
    App/
      NaumiAgentWorkbenchApp.swift
      AppState.swift
      AppEnvironment.swift
      AppRoute.swift
    API/
      WorkbenchAPIClient.swift
      WorkbenchEventClient.swift
      APIError.swift
      DTO/
        WorkbenchSnapshotDTO.swift
        MissionDTO.swift
        IssueDTO.swift
        TaskDTO.swift
        FailureDTO.swift
        EventDTO.swift
    Daemon/
      DaemonController.swift
      DaemonStatus.swift
      DaemonLogStore.swift
    Features/
      Dashboard/
        DashboardView.swift
        DashboardViewModel.swift
      Chat/
        ChatView.swift
      TaskMarket/
        TaskMarketView.swift
        TaskMarketViewModel.swift
      Worktrees/
        WorktreesView.swift
        WorktreesViewModel.swift
      Reviews/
        ReviewsView.swift
        ReviewsViewModel.swift
      Timeline/
        TimelineView.swift
        TimelineViewModel.swift
      Settings/
        SettingsView.swift
        SettingsViewModel.swift
    Components/
      StatusBadge.swift
      RiskBadge.swift
      LeaseChip.swift
      ContextHealthIndicator.swift
      AgentCard.swift
      IssueRow.swift
      FailureCardView.swift
      ApprovalPanel.swift
      AuditTimelineView.swift
    Localization/
      Localizable.xcstrings
    Resources/
      Assets.xcassets
    PreviewFixtures/
      workbench_snapshot_zh.json
      workbench_snapshot_en.json
```

## 4. AppState

AppState 是 SwiftUI 层唯一的共享状态根。

必须包含：

```text
selectedWorkspace
selectedSessionID
currentRoute
chatMessages
snapshot
connectionState
daemonStatus
locale
lastError
```

规则：

- `snapshot` 以后端返回为准。
- UI 不自行推导最终状态。
- WebSocket 断线后不继续信任本地增量事件，必须重新拉 snapshot。

## 5. Routing

使用 `NavigationSplitView` + route enum。

主导航：

```swift
enum AppRoute: String, CaseIterable {
    case dashboard
    case chat
    case taskMarket
    case worktrees
    case reviews
    case timeline
    case settings
}
```

中文默认显示：

```text
总览
对话
任务市场
工作区
审查
时间线
设置
```

英文显示：

```text
Dashboard
Chat
Task Market
Worktrees
Reviews
Timeline
Settings
```

## 6. ViewModel 边界

每个页面一个 ViewModel。

ViewModel 只负责：

```text
从 AppState / APIClient 取数据
做 UI 级筛选和排序
触发 API command
处理 loading/error
```

ViewModel 不负责：

```text
业务状态机
权限判断最终结论
lease 过期计算的事实写入
validation 执行
```

这些必须在 Python Workbench Kernel。

## 7. API Client

`WorkbenchAPIClient` 负责 REST：

```text
fetchSnapshot(sessionID)
createMission(...)
claimIssue(...)
releaseLease(...)
runValidation(...)
createIntentLock(...)
createDecision(...)
resolveApproval(...)
fetchMessages(...)
sendMessage(..., workbenchIssue: ...)
```

Chat 页通过 `sendMessage(sessionID:content:workbenchIssue:)` 访问 `POST /sessions/{session_id}/messages`。普通对话传 `workbenchIssue=nil`；对话转任务传 `ChatIssueDraftDTO`，后端返回 assistant message，并在 metadata 中带回 `workbench_issue` 和 `workbench_snapshot`。

对话转任务第一版固定走非流式语义。SwiftUI 可以显式发送 `stream=false`，也可以在 `workbenchIssue` 非空时省略 `stream`；后端会按非流式处理。只有显式 `stream=true` 且 `workbenchIssue` 非空时才返回 400。

Chat 页通过 `fetchMessages(sessionID:page:pageSize:)` 访问 `GET /sessions/{session_id}/messages`。连接成功、切换会话和进入 Chat 页时都可以刷新 `AppState.chatMessages`，并保留消息 metadata 里的 Workbench 任务联动状态。

`WorkbenchEventClient` 负责 WebSocket：

```text
connect(sessionID)
receive workbench/event
handle disconnect
reconnect with backoff
request fresh snapshot after reconnect
```

## 8. Snapshot/Event 同步策略

定案：

```text
snapshot 是真相
event 是增量提示
重连后必须重新拉 snapshot
```

启动流程：

```text
App launch
  -> load workspace registry
  -> check daemon status
  -> fetch capabilities
  -> fetch snapshot
  -> connect event stream
```

断线流程：

```text
WebSocket disconnected
  -> mark connection stale
  -> exponential reconnect
  -> reconnect success
  -> fetch snapshot
  -> resume event stream
```

## 9. 国际化

定案：

```text
默认语言：zh-CN
保底语言：en-US
SwiftUI 使用 String Catalog: Localizable.xcstrings
```

后端返回：

```text
machine-readable code
event type
enum value
optional localized default message
```

前端负责翻译：

```text
navigation
button
tab
status label
empty state
confirmation copy
settings label
```

禁止硬编码用户可见文案。

## 10. Preview 和 Mock 数据

每个页面必须有 SwiftUI Preview。

Preview 使用：

```text
PreviewFixtures/workbench_snapshot_zh.json
PreviewFixtures/workbench_snapshot_en.json
```

这样 UI 开发不依赖后端实时运行。

## 11. 测试策略

SwiftUI Shell 测试分层：

```text
DTO decode tests
API client tests with URLProtocol mock
ViewModel tests
Snapshot fixture tests
Localization key coverage tests
Manual UI smoke
```

MVP 不强求完整 UI automation，但必须有 DTO 和 ViewModel 测试。

## 12. 迁移路径

MVP：

```text
SwiftUI App connects existing localhost API
```

Phase 3：

```text
SwiftUI App starts and monitors local daemon
same WorkbenchAPIClient
same DTOs
same snapshot/event contract
```

完整产品：

```text
Daemon may be bundled or managed
SwiftUI code unchanged except DaemonController implementation
```

因此不会发生重大重构。
