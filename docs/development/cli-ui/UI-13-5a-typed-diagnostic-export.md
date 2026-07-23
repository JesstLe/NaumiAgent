# UI-13.5a Typed 脱敏诊断包导出

## 1. 目标

在 UI-13.1a Typed Doctor Health 之上交付一个真实、可验证、默认不写文件的诊断导出闭环：

1. 用户先看到将要导出的文件、大小、摘要和隐私边界；
2. 用户使用该次预览的精确 `snapshot_sha256` 确认；
3. Runtime 只把同一份已预览 ZIP 原子写入平台原生 Naumi 状态目录；
4. New UI、Textual TUI、旧 CLI fallback 与 Agent Tool 复用同一构建和写入实现。

该切片是 CC-03 Doctor 组件对齐和 ARC-07 闭源发行支持诊断的最小前置，不提前实现 UI-13.4
Trace Viewer，也不把诊断包上传给维护者。

## 2. 非目标

- 不包含聊天正文、模型 reasoning、原始 DebugTrace、环境变量全集、API key、源码或任意工作区文件；
- 不接受用户指定输出路径，避免越权写入和路径穿越；
- 不在 `/doctor` 或导出时运行付费模型 live probe；
- 不执行联网发送、工单上传、自动修复、软件安装或文件删除；
- 不把 UI-13.5a 宣称为完整 UI-13.5，后续 trace attachment 与显式共享仍需独立设计。

## 3. 权威模型

`src/naumi_agent/ui/doctor_export.py` 是跨表面的唯一导出权威。它接收已经受限的
`DoctorHealthSnapshot`，生成：

- `DoctorExportPreview`：来源快照、manifest、Bundle SHA-256、总大小和 3 个文件的摘要；
- `DoctorExportPlan`：进程内不可变的预览与 ZIP bytes，不进入协议；
- `DoctorExportReceipt`：实际本地路径、写入 Bundle/来源摘要、大小和是否复用已有文件。

ZIP 固定只包含：

| 文件 | 内容 |
| --- | --- |
| `health.json` | typed Health 状态、稳定诊断码、归因与脱敏声明 |
| `README.txt` | 用途、隐私边界与来源快照 |
| `manifest.json` | 产品/平台版本、文件摘要和明确排除项 |

归档成员顺序、JSON key 顺序、ZIP 时间戳与成员权限固定；同一个 Snapshot 生成可比较的确定性
Bundle。最大归档为 512 KiB，超限失败关闭。

## 4. 隐私与文件安全

导出前再次经过 `OutputGuardrail` 与诊断导出专用规则，覆盖常见 key/token/password/
authorization、OpenAI 风格 key、GitHub token、Bearer/Basic 凭据。词法路径和 symlink 解析后的
真实路径都会按长度优先替换，避免 macOS `/tmp`→`/private/tmp` 一类别名泄露：

- 当前 workspace → `<workspace>`；
- Naumi 状态目录 → `<naumi-state>`；
- 用户 home → `~`。

写入固定落到 `resolve_naumi_state_home()/diagnostics`：

- macOS/Linux 使用 `0700` 目录、`0600` 文件；
- Windows 使用用户 Profile 下的平台状态目录并继承用户 ACL；
- 临时文件写入、`fsync`、`os.replace`、目录 `fsync` 组成原子提交；
- diagnostics 目录为符号链接时拒绝写入；
- 同名文件只有摘要完全一致才复用，摘要不一致时拒绝覆盖；
- 回执明确说明文件尚未上传或发送。

Windows 当前依赖 Profile ACL；显式创建/校验 Windows DACL 属于 UI-13.5b 的后续安全加固，不能把
POSIX mode 测试当作 Windows ACL 验证。

## 5. 预览与确认状态机

```text
Doctor Health Snapshot
        |
        v
 build deterministic plan
        |
        v
 preview(files, bytes, digest, privacy)
        |
        +-- refresh/facts changed --> invalidate
        |
        +-- exact snapshot digest --> write cached bytes --> receipt
        |
        +-- missing/stale digest --> refuse
```

Bridge、TUI 与 Agent Tool 都只保留一个进程内待确认 Plan。刷新 Doctor、再次预览、摘要不匹配、
事实变化或成功写入都会替换/清除旧 Plan。`write` 不重新构造另一份“看起来相同”的 ZIP，保证回执
Bundle 摘要就是用户预览的 Bundle 摘要。

## 6. 各表面行为

### 6.1 New UI

- `/doctor export` 打开 typed Health 页，先运行本地 Doctor，再自动请求 preview；
- 页面显示 3 个文件、大小、Bundle 摘要和隐私说明；
- 第一次 `e` 在没有 preview 时只生成 preview；已有 preview 时才携带精确来源摘要请求写入；
- Doctor 刷新或新的 Health Snapshot 会使旧 preview 失效；
- `doctor/export/result` 使用严格 schema，私有/多余字段不会进入 UI state；
- 写入完成显示本地路径和“已导出/已复用”，不会宣称已上传。

### 6.2 Textual TUI

- `/doctor export` 生成预览，不写文件；
- `/doctor export <snapshot-sha256>` 只接受当前进程内缓存 Plan 的精确摘要；
- 普通 `/doctor` 保持原有 Markdown 报告；
- 预览、写入和错误均显示中文可执行下一步。

### 6.3 CLI fallback

- `/doctor` 运行原有本地诊断；
- `/doctor export` 通过 Engine Tool Executor 生成预览；
- `/doctor export <snapshot-sha256>` 通过同一权限规则写入；
- 歧义参数、非 64 位十六进制摘要在 CLI 本地拒绝，不发送模型请求。

### 6.4 Agent Tool

`doctor_export_diagnostics` 只接受 `preview/write` 和可选精确摘要，不接受路径。它是低风险诊断工具，
但标记为非只读、非并发安全；所有模式仍经过 Engine 权限矩阵。`bypass` 全权限模式直接放行，其他允许
模式也不弹出第二次高风险确认，因为真正的写入门是固定目录、进程内 preview 和精确摘要三重约束。

## 7. 协议

JSONL v1 新增：

- client `doctor/export`：`action=preview|write`，write 必须携带小写 64 位 SHA-256；
- server `doctor/export/result`：`status=preview|written`，written 必须带与 preview 完全一致的 receipt；
- negotiation capability `doctor_export`；
- protocol contract 将 client action 标记为 control/audit，将 server output path 标记为
  sensitive/redaction-required。

旧 Bridge 不声明 capability 时，新 UI 不得假设支持导出；Textual TUI 为进程内共享实现，不伪造 JSONL
协商。

## 8. 验收证据

- 相同 Snapshot 产生相同 Bundle；解压后只有 3 个允许文件；
- fixture 中 secret、workspace/state/home 绝对路径、聊天、reasoning 与 raw trace 均不出现在解压内容；
- preview 阶段不创建 diagnostics 目录；
- 无 preview、错误摘要、Doctor facts 变化、symlink 目录和同名篡改文件均失败关闭；
- POSIX 目录/文件权限与原子复用可机械验证；
- Bridge、新 UI reducer/renderer/process、TUI、CLI fallback 和 Agent Tool 都覆盖 preview→write；
- capability manifest 的 Doctor 证据指向真实跨端测试；
- 运行真实本地 Doctor 后可在临时平台状态目录生成并读取 ZIP；
- 仅运行 UI-13.5a 相关小模块测试，不以本切片结果代表全量回归。

## 9. 后续

- UI-13.5b：用户显式选择加入的 bounded trace 片段、Windows DACL 校验、支持工单上传前第二次预览；
- UI-13.4：typed Trace Viewer、筛选、正文默认折叠和稳定事件关联；
- CC-03：补 source behavior inventory、语义映射、差异日志与同 fixture golden；
- ARC-07：闭源产物中的本地 support bundle 入口、第三方许可和升级失败诊断；
- UI-17：增加断连 uncertain、旧 Bridge typed downgrade 与恢复场景的发布 golden。
