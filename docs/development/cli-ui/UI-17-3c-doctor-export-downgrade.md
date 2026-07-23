# UI-17.3c Doctor Export Capability Downgrade

## 1. 目标

在 UI-13.5a 已交付的 typed 脱敏诊断包导出之上，补齐 New UI 与旧 Python Bridge 组合的
兼容闭环：

1. New UI 只在 `hello` 已协商 `doctor_export` 后发送 `doctor/export`；
2. 旧 Bridge 仍可返回 `doctor/health`，用户可以继续查看本地健康状态；
3. `/doctor export` 和页面内 `e` 在能力缺失时都只显示中文兼容提示，不发送 preview/write；
4. Python Bridge 在收到未协商的请求时，必须在构建 Plan 或创建 diagnostics 目录前失败关闭。

该切片复用 UI-17.3b 的共享 `event_capabilities` registry，不再为 Doctor 写第二份能力映射。

## 2. 依赖与非目标

依赖：

- UI-13.5a：确定性 Bundle、预览确认和固定状态目录写入；
- UI-17.3a：新旧 Bridge 的 typed feature 降级先例；
- UI-17.3b：client/server event → capability 的发布合同权威。

非目标：

- 不让旧 Bridge 获得其没有实现的导出能力；
- 不把 `/doctor export` 降级为普通聊天或共享 Slash 执行，因为旧 Bridge 可能同样不认识该命令；
- 不改变 Textual TUI 的进程内导出路径；
- 不实现断线后的 preview 恢复、跨进程 Plan 持久化或 UI-17.4 发布矩阵。

## 3. 兼容矩阵

| 前端 | Bridge | 行为 | 是否发送 `doctor/export` |
| --- | --- | --- | --- |
| New UI | 声明 `doctor_export` | Health → preview → 精确摘要 write | 是 |
| New UI | 未声明 `doctor_export` | 展示 Health 与升级提示，导出保持本地禁用 | 否 |
| New UI | hello 尚未完成 | 保留 Health 请求排队语义；导出动作失败关闭 | 否 |
| 旧 New UI | 当前 Bridge | Bridge 通用能力门返回 `protocol_capability_not_negotiated` | 否（执行层） |
| Textual TUI | 当前 Engine | 继续使用共享 Python authority | 不适用 JSONL |

## 4. 状态与 UX

New UI 通过 `requiredEventCapability("client", "doctor/export")` 查询共享合同，再结合 hello ack
得到四种状态：

- `available`：允许 preview/write；
- `pending`：协议尚未完成，提示未发送导出请求；
- `unsupported`：旧 Bridge 未声明能力，提示升级 Python Bridge；
- `unregistered`：本地发布合同缺失，视为产品安装不完整并失败关闭。

`/doctor export` 仍先打开 Doctor Health 页面，避免能力缺失把全部诊断能力一起降级。兼容提示使用
黄色 `兼容模式`，与真实导出错误的红色 `导出失败` 分开。刷新 `r` 会保留用户的导出意图：新 Bridge
重新生成 preview，旧 Bridge 重新显示兼容提示；按 `e` 不会绕过能力门。

Evaluation Lane 同时改用同一个 capability 状态查询，删除前端第二份手写的协商判断；其既有
typed→Slash 降级行为不变。

## 5. 安全不变量

- capability 未协商时，Node debug trace 中不存在 `doctor/export` send record；
- capability 未协商时，Bridge 不调用导出 Plan builder，也不创建 diagnostics 目录；
- `doctor/health` 不绑定 `doctor_export`，旧 Bridge 组合仍保留只读诊断；
- 兼容提示不伪装成导出失败，也不声称生成或写入了文件；
- server result 仍使用 request id 关联，过期或主动注入的结果不能形成当前回执；
- Textual TUI 不伪造 JSONL 协商状态，继续依赖本地 Python 实现。

## 6. 验收证据

- Node state test 覆盖 pending、unsupported、自动 preview、页面 `e` 和刷新；
- Node protocol test 证明 `doctor/export` 与 `doctor/export/result` 均绑定 `doctor_export`；
- renderer test 区分黄色兼容提示与红色真实错误；
- 真实 Node UI 子进程连接不声明能力的旧 Bridge fixture，打开 Health 且两次操作均无
  `doctor/export` 发送；
- Python Bridge test 在真实 hello negotiation 后证明拒绝发生于 Plan/写盘之前；
- 只运行上述 UI-17.3c 小模块测试，不以此证明 UI-17.3 或完整发布门完成。

## 7. 当前边界与下一步

UI-17.3 仍为 partial。Doctor Export 与 Evaluation Lane 已具备独立的能力降级闭环，但未知关键
server event 分类、snapshot/cursor 补发、断线后的 uncertain 恢复，以及 OS/Python/Node/终端
升级回滚矩阵仍未完成。下一切片应再次跨文档比较这些发布门与 Harness 恢复依赖，不线性扩张
Doctor 功能。
