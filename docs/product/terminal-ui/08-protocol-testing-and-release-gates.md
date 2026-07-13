# 08 协议、测试与发布门禁

## 1. 目标

把 Terminal UI 从“前后端碰巧配合”提升为有版本、有契约、有乱序和恢复语义的产品接口，并用分层测试证明真实可用。

## 2. Bridge v2 信封

所有事件统一包含：

```json
{
  "protocol_version": 2,
  "type": "run/state",
  "event_id": "evt_...",
  "request_id": "req_...",
  "session_id": "ses_...",
  "run_id": "run_...",
  "seq": 42,
  "timestamp": "2026-07-13T10:00:00+08:00",
  "payload": {}
}
```

客户端请求可没有 `seq`，但必须有唯一 `request_id`。服务端持久事件必须有 `event_id` 和会话内单调 `seq`。短暂 UI 通知可标为 `ephemeral=true`，不得用于恢复执行事实。

## 3. 新增协议能力

- `capabilities` 握手：声明 Inspector、命令页、完成收据、恢复和本地化能力。
- `run/state`：规范化运行状态机。
- `completion/receipt`：结构化完成收据。
- `session/resume`：携带最后确认序号和未确认请求。
- `page/snapshot` 与 `page/event`：为 tasks/agents/workbench 提供统一分页快照和增量。
- `cancel/request` 与 `cancel/resolved`：明确取消竞态结果。

现有 v1 事件通过 Bridge 适配到 v2；迁移期协议契约同时记录兼容映射。

## 4. 错误模型

错误包含稳定 `code`、本地化 `message_key`、安全参数、是否可重试和关联请求。堆栈与原始异常只写 debug log。客户端对未知事件显示可忽略诊断，不终止整个 UI；对破坏顺序或权限安全的未知状态则进入安全降级。

## 5. 测试金字塔

### Node 单元测试

Reducer、输入缓冲、Unicode 宽度、时间线合并、响应式布局、路由、快照迁移、权限焦点和完成收据渲染。

### Python 单元测试

Bridge handler、事件适配、会话重放、请求幂等、收据聚合、命令入口和本地化键完整性。

### 契约测试

由 `protocol-contract.json` 生成正反例，Node 和 Python 同时校验。覆盖未知字段、缺失必填字段、版本范围、重复事件、序号缺口和错误脱敏。

### 进程集成测试

真实启动 Node + Python Bridge，使用临时 SQLite 和真实引擎测试替身，不 mock JSONL 管道。验证握手、提交、流式、权限、取消、恢复和关闭。

### 真实场景验收

在真实 Git 仓库执行只读分析、受控文件编辑、定向测试、失败恢复和完成收据。不得用预制 UI 数据替代。

## 6. 性能门槛

- 本地按键到重绘 P95 小于 50 ms。
- Bridge 事件到可见更新 P95 小于 100 ms。
- 10,000 条时间线事件下滚动和输入不明显卡顿。
- 常规会话恢复首屏小于 500 ms，完整增量可后台补齐。
- 内存随折叠历史受控，不无限保留完整渲染行缓存。

性能测试必须记录终端尺寸、事件数量和机器环境。

## 7. 安全与隐私门禁

- 日志、收据和工具卡执行敏感字段脱敏测试。
- bypass 范围必须由后端策略确认并可撤销。
- UI 不能通过构造事件绕过 PermissionChecker。
- 路径、命令和外部内容按纯文本渲染，防止 ANSI/终端控制序列注入。
- 崩溃快照不保存密钥、Cookie、完整环境变量或隐藏推理。

## 8. 发布门禁

发布候选必须同时满足：

1. 受影响模块的 Node/Python 定向测试全部通过。
2. 完整 Node 测试、`ruff check src/`、`pytest tests/ -x` 通过。
3. 源码态与安装态启动验收通过。
4. macOS 常见终端至少验证 Terminal.app、iTerm2 和 Codex 内置终端。
5. 中文默认和英文切换完成核心链路验收。
6. 无 P0/P1 缺陷；所有已知不足写入发布说明。
7. 默认入口回滚开关经过演练。

## 9. 完成判定

只有测试结果、真实场景记录、性能数据和发布清单均有证据时，Terminal UI 产品化才可标记完成。文档完成、页面可截图或 import 成功都不构成完成证据。
