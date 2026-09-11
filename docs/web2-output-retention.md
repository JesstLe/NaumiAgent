# Web2 输出资源生命周期

## 目标

`output_publish` 将图片、PDF、CSV、JSON、Markdown 等文件以 SHA-256 内容哈希保存到
会话库旁的 `output-assets`。同一内容只保存一份，因此资源不能简单随单个会话删除。
本模块增加有界的 mark-and-sweep 清理：先扫描所有持久化引用，再只删除达到保留时间、
没有任何引用的托管文件。

## 引用保护

扫描范围包括：

- Session 消息与摘要；归档会话仍计入引用，归档操作不会破坏历史图片。
- Chat run 的步骤摘要、工具详情与 metadata；工具完成但最终回答尚未形成时仍保护资源。
- Chat run artifact、source 与 completion receipt 的文本字段。
- 多个会话引用同一哈希时，只要任意引用仍存在，文件就不会进入清理候选。

扫描不解析或执行消息内容，只匹配受控的
`/api/v1/output-assets/{sha256}.{ext}` 路径。引用扫描或资源扫描达到上限、SQLite
损坏或旧表结构无法可靠读取时，执行端 fail closed，不删除任何文件。

## 清理规则

- 只处理名称满足托管哈希格式的普通文件。
- 符号链接、目录、未知文件和发布临时文件均不触碰。
- 默认保护最近一小时内发布或重复发布的资源，覆盖文件生成到消息/工具结果提交之间的窗口。
- 发布与清理在当前 daemon 内共享互斥锁；删除前再次核对大小、mtime 与文件类型，状态变化则跳过。
- 单次默认最多删除 100 个，硬上限 500 个；候选按最旧优先排序。
- 每个文件通过文件系统原子 unlink 独立删除；单个删除失败不会扩大到其他文件，并在结果中列出。

## 双通道入口

- Agent 工具：`output_asset_retention_preview`（只读）与
  `output_asset_retention_run`（破坏性、需要确认）。
- 用户命令：`/output retention-preview [最小保留秒数]` 与
  `/output retention-run [最小保留秒数] [单次删除上限]`。
- CLI、TUI 与 Web2 命令接口继续走同一 `_handle_command` 和 Tool 执行链路。

## 验证

真实临时 SQLite 与文件系统场景覆盖：活动会话引用、归档会话引用、共享哈希、仅 run step
引用、孤儿删除、最近资源延迟、非托管文件、目录伪装、删除上限、重复发布续期、损坏数据库
fail closed、扫描上限、删除前状态变化、命令双通道和 Engine 注册。输出资源定向测试为
33 项通过，`ruff check src`
通过。

当前 `config.yaml` 数据目录的真实执行结果：清理前有 4 个托管资源，其中 3 个受持久化
引用保护，1 个 112 B 的旧孤儿 SVG；有界清理删除该孤儿资源，未发生状态变化跳过或删除
错误。清理后剩余 3 个资源均仍受引用保护，逐文件重新计算 SHA-256 与文件名一致。
证据保存在 Git 忽略的 `.naumi/data/output-retention-real.json`。

## 当前边界

- 当前 daemon 内的发布与清理互斥；不支持两个独立 daemon 同时写入同一 session data 目录。
  项目运行模型本身也要求一个数据目录由一个 daemon 管理。
- 清理不会修改会话正文，也不会主动修复手写但不存在的资源链接。
- Session retention 删除会话后，资源会在下一次显式 output retention 清理中回收；当前没有独立的
  output retention 周期 worker。
