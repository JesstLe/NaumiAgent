# NaumiAgent Mac Agent Workbench Local Security and Workspace Authorization

> 本文定案本地安全、workspace 授权、危险操作确认和中英双语错误边界。

## 1. 结论

Workbench 默认只管理用户显式授权的 workspace。

```text
User selects workspace folder
  -> App records WorkspaceRegistry entry
  -> Backend validates repo
  -> Workbench state scoped to workspace/session
```

MVP 可以先使用非 sandbox 直连文件路径；完整产品必须支持 macOS security-scoped bookmark 或等价授权机制。

## 2. Workspace Registry

必须预留数据模型：

```text
workspace_id
name
path
git_root
authorization_state
default_session_id
last_opened_at
created_at
updated_at
```

状态：

```text
authorized
missing
permission_denied
not_git_repo
needs_reauthorize
```

## 3. 授权流程

MVP：

```text
用户选择本地目录
  -> App 显示路径
  -> 后端验证 git root
  -> 保存 workspace registry
```

完整产品：

```text
用户选择目录
  -> 保存 security-scoped bookmark
  -> 每次启动恢复权限
  -> 权限失效时要求重新授权
```

## 4. 本地 API 安全

规则：

```text
bind 127.0.0.1 only
write API requires token
token stored in Keychain
no token in logs
no token in audit event
```

MVP 可以使用 dev token，但 API 形状必须和未来一致：

```http
Authorization: Bearer <token>
```

## 5. 危险动作确认

以下动作必须由 SwiftUI 显示 confirmation sheet：

```text
force remove worktree
delete mission
delete audit data
approve high risk
approve critical
run non-standard validation command
change governance policy
change model/provider config
stop daemon while tasks active
```

确认弹窗必须包含：

```text
动作名称
影响对象
风险说明
是否可恢复
需要输入确认文本的条件
```

critical 动作要求用户输入确认文本：

```text
确认删除
CONFIRM
```

## 6. 后端权限检查

UI 确认不是安全边界，后端仍必须检查权限。

后端必须返回：

```json
{
  "code": "permission_denied",
  "message": "当前权限不允许删除 dirty worktree。",
  "details": {
    "reason": "worktree_dirty",
    "worktree": "issue-3-market"
  }
}
```

SwiftUI 根据 `code` 本地化显示文案。

## 7. Validation Command 安全

默认 allowlist：

```text
ruff check
pytest
python3 -m pytest
npm test -- <specific test>
```

禁止默认允许：

```text
rm
curl | sh
sudo
chmod -R
git reset --hard
git clean -fd
```

如果用户需要自定义验证命令，必须在 Settings 中显式添加，并进入 audit log。

## 8. Worktree 安全

规则：

- dirty worktree 默认不可删除。
- kept worktree 不可被自动清理。
- missing worktree 只清理 metadata，不假装删除文件。
- force remove 必须二次确认。
- 删除前显示 dirty file count 和 commits ahead。

## 9. Secret 处理

禁止进入：

```text
AuditEvent payload
FailureCard detail
ValidationRun output
UI timeline
copied diagnostic summary
```

需要脱敏：

```text
API keys
Bearer tokens
private keys
passwords
cookie values
authorization headers
```

## 10. i18n 错误策略

后端返回：

```text
code
default_message_zh
details
```

SwiftUI 显示：

```text
zh-CN: localized by code or default_message_zh
en-US: localized by code fallback
missing key: show code
```

这样现在中文优先，未来英文不会推倒错误模型。

## 11. 平滑过渡保证

MVP 到完整产品不会重构的原因：

1. WorkspaceRegistry 从第一天存在，即使只管理一个 repo。
2. API token header 从第一天存在，即使 MVP token 简单。
3. 后端权限检查从第一天存在，UI confirmation 只是体验层。
4. dangerous action code 从第一天结构化，未来只增强策略。
5. i18n 使用 code 驱动，不依赖后端硬编码英文/中文。
