# 个人工具 Provider Schema 显式类型

日期：2026-11-06

## 问题和证据

基线：`ac6c7a731558adcaeab8437e9af34d77fbf6cdd9`。
事务：`.naumi/transactions/personal-provider-schema-20261106/`。
修改前冻结、重开 3179 个原文件及两份独立源码副本。

上一模块全仓真实 API 请求返回 HTTP 400：
`properties.operation: type is not defined`。原失败 Run
`21fc05a86875` 和 session `0b7e3550fe88` 保留，计费 UNKNOWN，重放 0。
错误没有点名 Tool，不能将静态匹配写成提供方已确认具体工具。

本次从实际隔离 Engine 导出全部 280 个已注册工具进行递归检查，
共发现 16 个 enum/const 节点没有显式类型，全部属于以下三个工具：

- `personal_items`：operation、state、schedule.mode、schedule.misfire_policy、
  policy.tool_names.items、policy.artifact_operations.items、policy.model_tier，7 处。
- `personal_connections`：5 个操作分支的 operation const。
- `personal_actions`：4 个操作分支的 operation const。

这些节点的允许值全部是字符串。原 Schema 符合通用 JSON Schema，
但部分模型提供方还要求显式类型；Tool 原样导出，因此业务本地验证通过
不代表提供方会接收这份 schema。

## 实现合同

只给这 16 个节点增加 `type: string`，不增加 Router 层的通用推断、
不扁平化 oneOf、不更改枚举/常量值、必填字段、嵌套定义或业务 dispatcher。
`schedule.fold` 保持 `type: [integer, null]`，不错误地收窄为字符串。

人工账户授权、批准日程写入和模型草稿权限保持原边界；模型接口仍不提供
approve、execute、OAuth 或身份字段。用户手动/API/CLI/TUI 与 Agent 工具
继续共享原服务，不新建第二套业务路径。

## 验证设计

- 导出合同：实际 Tool.to_openai_tool，包括嵌套 defs/oneOf 的 enum/const 类型。
- 参数语义：对每个改变节点和各操作代表请求比较冻结前后 JSON Schema
  接受集合；覆盖错误类型、空输入、额外字段、身份与人工审批伪造。
- 实际本机 HTTP：真实 ModelRouter 发送三个工具原 schema，本机协议端点
  先检查类型，再返回工具调用；原工具解析并读取真实 SQLite 服务，再将
  结果通过第二次 HTTP 送回。没有真实账户连接或外部日程写入。
- 集中相邻回归：事项 API/命令、日程 API/命令、权限、审批与执行路径。
- 原件哈希、源码副本回滚、后改零写保护及完整字面命令输出记录于
  `VERIFICATION.txt`；既有失败不覆盖、不改写。

## 自审和验收层级

### 本轮实际结果

- 原导出探针：16 处缺失，退出 1；原源码运行新增合同测试为
  **12 failed / 24 passed**，真实本机 HTTP 也在首个模型请求返回预期 400。
- 修改后实际 280 工具清单无这类缺失；只有上述三个导出 Schema 的 SHA 改变，
  其余 277 个逐个相同。
- 集中相邻回归：**443 passed / 1 failed in 176.87s**。唯一失败是原有
  `test_actual_engine_composition_router_leaf_tools_and_shared_cli_tui`
  将全 API 路由数固定为 213，而当前实际为 218；冻结原源码也复现同样失败。
  本次不修改该断言或 API 路由，不把这份回归报告标为全部通过。
- 新本机 HTTP 场景的首次修改版在只读窗口内发现既有 lazy collaboration
  journal 的首次建表，原失败保留。仅修正测试准备：在窗口前先通过原只读
  入口准备空 journal，随后再次对完整逻辑数据库做前后相等断言。
- 新独立真实提供方验证：`schema-b212e2bb2856472f99f991701f377dda`，
  session `f9becebe8e96`，原 Item `pi_6f89e591e55248e8adc54b6e49d82695`。
  配置的 `kimi-for-coding` 接收三份确切导出 Schema，第一次响应提出
  `personal_items list`，真实 Tool 读取该原 Item，第二次响应返回
  **`SCHEMA-LIVE-7391`**。两次 Router 响应完成、数据库逻辑状态不变、
  Engine 已关闭、旧请求重放 0。该验证未创建 ChatRun，不伪称新的完整 Agent Run。
- 返回的用量具备 transport evidence；成本字段属于 fallback rate-card 估计，
  不当作提供方实际账单。原失败的计费 UNKNOWN 保留。

本机与真实提供方脚本在 Router 返回后调用实际 Tool parser/execute 与
真实服务，证明这一链路；它们没有替代 Engine.execute_tool 的完整权限循环。
权限、人工拒绝和草稿审批合同另由集中相邻测试覆盖。

本次是工具请求合同修复，不代表日程 Google 账户、所有 Provider、UI、
部署或完整产品已通过。完整 24 项范围继续保留。原真实 HTTP 400 未被
本机模型协议测试替代或重放；本次新的真实请求已有独立身份与证据。
整个 280 工具目录的真实提供方接收、全仓 `tests/ -x` 在本次修改后的重新
验收仍未执行，不能由三个工具的成功调用外推。
