# NaumiAgent Mac Agent Workbench Packaging and Distribution

> 本文定案 Mac App 从本地开发版到完整分发版的路径。  
> 决策：MVP 不做 App Store，不立即 bundle Python runtime；先做可开发、可验证、可平滑升级的本地产品。

## 1. 结论

发布路径分四步：

```text
Dev Build
  -> Internal Signed Build
  -> Notarized Direct Distribution
  -> Auto Update
```

不优先：

```text
Mac App Store
```

原因：

- 本产品需要本地 workspace、shell、git、pytest、daemon。
- App Store sandbox 会显著增加早期复杂度。
- 先验证 local-first Agent OS 更重要。

## 2. Dev Build

阶段：

```text
Phase 2 SwiftUI Shell MVP
```

特点：

```text
Xcode local run
connect localhost backend
no notarization
no bundled runtime
manual backend or app-managed light daemon
```

目标：

- 快速验证 UI。
- 快速验证 API contract。
- 快速迭代中文默认和英文 fallback。

## 3. Internal Signed Build

阶段：

```text
Phase 4 Product-grade Local App
```

能力：

```text
Developer ID signing
local daemon manager
Keychain token
workspace registry
logs
settings migration
```

仍不要求：

```text
auto update
cloud sync
App Store
```

## 4. Notarized Direct Distribution

阶段：

```text
Phase 5 Distribution
```

分发方式：

```text
GitHub Releases
download .dmg or .zip
Developer ID signed
Apple notarized
```

用户安装体验：

```text
download
drag to Applications
first launch
select workspace
start local daemon
```

## 5. Auto Update

推荐：

```text
Sparkle
```

规则：

- App update 和 daemon compatibility 要检查。
- 更新前显示 release notes。
- 更新后重新检查 daemon status。
- 重大 schema migration 必须备份。

## 6. Python Runtime 分发策略

### 6.1 MVP

```text
依赖本机开发环境
naumi-agent command or python -m naumi_agent
```

### 6.2 中期

```text
managed local daemon binary
installed separately or downloaded by app
```

### 6.3 完整产品

两种候选：

| 策略 | 说明 | 采用时机 |
|------|------|----------|
| bundled Python runtime | App 包含 Python runtime 和 NaumiAgent | 用户安装体验优先时 |
| managed daemon package | App 管理独立 daemon 包 | 需要 daemon 独立更新时 |

当前定案：

```text
先 managed daemon package，后评估 bundled runtime。
```

## 7. 数据迁移

需要版本化：

```text
Workbench SQLite schema
WorkspaceRegistry
Settings
Localization resources
Daemon protocol version
```

迁移原则：

- migration 必须可重复。
- migration 前备份。
- migration 失败不能破坏原数据。
- App 和 daemon protocol mismatch 时必须阻止危险操作。

## 8. 文件位置

建议：

```text
Application Support:
~/Library/Application Support/NaumiAgentWorkbench/

Logs:
~/Library/Logs/NaumiAgentWorkbench/

Caches:
~/Library/Caches/NaumiAgentWorkbench/

Keychain:
service = NaumiAgentWorkbench
account = local-daemon-token
```

Repo/worktree 数据仍由 NaumiAgent backend 管理，不放进 App bundle。

## 9. 卸载策略

App 删除时默认不删除：

```text
workspace
worktree
audit log
session db
```

设置页可以提供：

```text
Reset App Settings
Clear Cache
Export Audit Logs
Forget Workspace
```

高风险清理必须二次确认。

## 10. 平滑过渡保证

不会重大重构的原因：

1. SwiftUI 从第一天通过 API 访问 daemon。
2. Daemon 从第一天暴露 capabilities/version。
3. Packaging 只替换 daemon 获取方式，不替换 Workbench API。
4. Settings 和 workspace registry 从 MVP 就按 product-grade 形状设计。
5. Direct distribution 可以在不改变 app architecture 的情况下加入 signing/notarization/Sparkle。
