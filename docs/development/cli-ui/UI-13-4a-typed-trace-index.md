# UI-13.4a Typed 隐私有界 Trace 索引

## 1. 目标

在不暴露用户正文、模型输出、reasoning、工具参数或异常堆栈的前提下，把一轮本地
`DebugTrace/events.jsonl` 投影为可筛选、可刷新、跨界面一致的 typed 事件索引。

该切片只交付 UI-13.4 Trace viewer 的最小可信前置：事件类型、严重度、时间、安全标识符、
低风险摘要与 Snapshot。它不提前实现 raw trace 展开、跨运行选择、流式追尾或诊断包附件。

## 2. 唯一权威与调用路径

`src/naumi_agent/ui/doctor_trace.py` 是投影权威，`DoctorTraceIndexTool` 是用户命令与 Agent
自主调用共用的执行入口：

```text
DebugTrace events.jsonl
        |
        v
bounded stable tail read (max 2 MiB, retry 2)
        |
        v
privacy projection + query + newest-first limit
        |
        +--> Agent Tool / CLI `/doctor trace [query]`
        +--> Textual TUI fallback (same CLI tool path)
        +--> Python Bridge `doctor/trace/result`
                    |
                    v
              New UI Doctor page
```

New UI 只校验、保存和渲染 typed payload，不直接读取文件，也不重新判断严重度或脱敏正文。
Bridge 只允许索引当前 Bridge 的精确 `run_id`；Tool 只允许读取配置状态目录下最新的合法运行，
两者都不接受任意文件路径。

## 3. Typed contract

Schema v1 的顶层字段包括：

- `status`: `ready | degraded`；
- `diagnostic_code`: `trace_index_ready | trace_source_changing | trace_malformed_lines`；
- `run_id`、`interface`、`assessed_at`；
- `query`、`limit`、source/window bytes、窗口/匹配/损坏行计数与 `truncated`；
- `entries`: newest-first，最多 200 项；
- `snapshot_sha256`: 对全部有界投影事实计算的确定性摘要；
- `privacy_notice`: 明确说明被折叠内容。

每个 entry 只包含 byte cursor、ISO 时间、事件类型、严重度、低风险摘要，以及白名单中的
`run_id/request_id/call_id/task_id/session_id/agent_id`。协议注册能力为
`doctor_trace_index`，客户端事件为 `doctor/trace`，服务端事件为
`doctor/trace/result`。旧 Bridge 未协商该能力时，New UI 保留 Doctor Health 页面并显示兼容提示，
不会发送一个注定失败的请求。

## 4. 隐私、边界与并发规则

- 每次最多读取文件末尾 2 MiB；单行最多 64 KiB；结果默认 80、最大 200 项；query 最大 128 字符。
- 超过窗口时丢弃首个不完整行并明确标记 `truncated`，不扫描整个历史文件。
- 读取前后比较 size 与 mtime；最多重试两次。仍在变化时返回 `degraded`，不伪装稳定。
- JSON 损坏、非对象和超长行只计数，不把原文或解析异常内容带入结果。
- `text/content/message/trace/reasoning/arguments/args/prompt/task` 永不进入 typed payload。
- 事件名、摘要原子、时间和标识符分别经过严格格式白名单；形似 key/token/password/secret/
  authorization/bearer 的值即使字符合法也会折叠。
- run 目录、manifest 和 events 文件均经过解析后边界检查；路径穿越和 symlink escape 失败关闭。
- 查询只匹配已脱敏投影，不匹配 raw body，因此不能借筛选侧信道探测隐藏正文。

## 5. 用户体验

### New UI

- `/doctor trace [筛选]` 打开 Doctor 页面并发出相关请求；`t` 使用同一筛选刷新。
- 页面显示 ready/degraded、窗口与匹配计数、Snapshot、隐私说明和颜色区分的严重度。
- loading、空结果、兼容降级、typed error 均有独立文案；私有额外字段被前端 normalizer 丢弃。

### Textual TUI 与 CLI fallback

- 两端接受同一 `/doctor trace [筛选]` 语法并通过 Engine `execute_tool()` 执行权限策略。
- Markdown 结果展示相同排序、计数、诊断码与隐私声明；不维护第二份索引逻辑。
- Agent 可调用只读、并发安全的 `doctor_trace_index` Tool，参数只有 `query` 与 `limit`。

## 6. 验收矩阵

| 场景 | 验收结果 |
| --- | --- |
| 用户正文、模型输出、reasoning、工具 args、异常 message/trace 含 secret | typed payload 与 Markdown 均不出现原文 |
| 元数据值形似 API key/token/password | 即使满足字符白名单仍折叠 |
| 按 error、event type、call id 查询 | 只在脱敏投影上匹配，结果 newest-first |
| 文件超过 2 MiB 或结果超过 limit | 有界读取/返回并标记 `truncated` |
| 文件在读取时继续追加 | 最多重试两次，仍变化则 `degraded` |
| 损坏 JSON、超长行、空目录、非法 run id、symlink escape | 稳定诊断码或安全拒绝，无 raw 内容泄漏 |
| New UI 与当前 Bridge | 协商能力后返回 correlated typed result |
| New UI 与旧 Bridge | 显示兼容提示，不发送请求 |
| TUI/CLI/Agent Tool | 全部走同一 `execute_tool()` 与投影 authority |
| 相同稳定文件与参数重复索引 | `snapshot_sha256` 一致 |

## 7. 非目标与后续

- UI-13.4b：有界实时追尾、暂停、cursor 恢复和背压；
- UI-13.4c：显式选择历史 run，并继续限制在 Naumi 状态根目录；
- UI-13.4d：经单独授权的正文局部展开与逐字段脱敏，不允许默认展开；
- UI-13.5b：把用户明确选中的 typed trace attachment 加入诊断包；
- UI-13.6：基于稳定诊断码提供安全、可逆的修复动作。

因此 UI-13 仍为 `partial`，本切片不能被表述为完整 Trace viewer 或完整诊断系统。
