# PA-02 Fresh-send 记忆边界验证

## 验证对象

`tests/unit/test_personal_memory_runtime.py` 独立于 Runtime/Service 实现编写。使用真实 `PersonalMemoryService`、`PersonalAgentService` 与 catalog、`SessionStore`、`PersonalMemoryStore` SQLite；不 mock 这些实际权威组件。

语义索引是显式 unavailable 测试 port：upsert/delete/query/ensure_current 都报告不可用，不生成假的向量或语义分数。资料和事项边界是可撤权的受控 port，用于精准触发来源撤权和 item 绑定；实际资料系统、ONNX embedding 质量、模型和浏览器由父任务独立验收。本文件不启动服务、不请求模型。

## 覆盖矩阵

静态展开为 23 个 cases，实际计数以 pytest 收集结果为准：

| 边界 | 验证结果要求 |
| --- | --- |
| Fresh-send | 实际 Tool 返回 exact refs，最终发送时才解析正文；原 messages、full_history 和会话 SQLite 不新增召回正文 |
| 删除/纠正 | 旧引用触发本轮授权变化；下一次 prepare 不再带旧正文。纠正只发送新修订，删除只保留无正文状态 |
| 分享撤权 | 真实 grant 分享后可读取；真实 revoke 后旧 grant/ref 不泄漏正文 |
| 来源撤权 | 资料授权 port 撤权后，原记忆和来源资料引用均不再进入发送上下文 |
| 禁用记忆 | 带/不带 metadata 的 Tool refs 均清空，旧派生 system 块移除，真实用户同名原文保留 |
| Scope | item > workspace > personal；同范围自己拥有的记录胜过分享；personal 跨工作区但不隐式跨 Agent |
| 自动提取 | 真实 extractor 的 user/assistant 候选分别保留真实消息位置/SHA/来源；全部 proposed，无人工确认；运行重试不重复创建 |
| 提取防护 | isolated、disabled、私有资料、缺失 run、缺失原消息均不冒造用户来源 |
| Turn 生命周期 | begin_turn/reset 清除旧 refs/资料 refs/query；prime 使用新查询和真实来源位置 |
| 上下文限额 | 整条遗漏，不截断一条记忆制造不完整含义 |
| Compaction | 实际 scoped adapter → Service → SQLite，只写 compaction/assistant-or-unknown 候选；raw legacy trap 零访问 |
| 兼容入口 | 无运行/禁用时拒绝 compaction；禁用后空 query search 不绕过召回开关；模型不通过兼容入口删除记忆 |
| 权威丢失 | 实际 memory DB 丢失报告 storage_error，不重建成空记忆并假装成功 |
| 旧输出 | 旧全局记忆 Tool 正文清除，但用户原话不被删除 |

## 自审发现及交接

实现阅读阶段发现并交给对应实现负责人：

1. 原 extractor 合并 user/assistant 且缺少 source_role，auto_capture 默认归为 user，会把 assistant 项目结论错记为用户来源。已要求拆分和真实 source_quote，本测试保持 assistant 预期，未通过降级断言接受旧实现。
2. 禁用记忆时没有 metadata 的 raw refs envelope 仍保留引用；metadata 有效路径也需要只在 ephemeral 副本中清 refs，不突变持久原消息。
3. ScopedMemoryAdapter.store 缺失实际 propose_compaction Service 方法，兼容 compaction 入口会失败；对应方法由 Service 负责人补齐。
4. ScopedMemoryAdapter.search 空 query 直接返回正文，绕过 enabled 判断；实现负责人补齐开关。

## 运行记录

本子任务已运行 `ruff format` 和 `ruff check tests/unit/test_personal_memory_runtime.py`，输出 `All checks passed!` / exit 0。

初次交接冻结时，pytest 尚未运行：Service 正在完成递归来源/分享授权，父任务选择等待统一冻结后集中执行 runtime、engine、index、Service、接口、Store 与既有记忆回归，避免测试读到半完成的并发编辑。本子任务不重复启动这一批命令。最终 pytest 结果以父任务 transaction 下对应真实执行记录为准。


## 2026-11-05 Root 最终集成

本文件的全部 runtime cases 已包含在实际通过的 Root 集成组中。全相关后端为 **1902 passed / 1 failed / 2 skipped**，唯一失败为未修改的 PA-01 ItemStore 首次并发 WAL 建库，已在 HEAD 隔离原源码复现。后续 provider-schema、只读 legacy、真实 Engine 发送/压缩/来源持久 pin 等最后集成为 **434 passed / exit 0**，包含本文件全部 cases；两组不叠加计数。

命令、完整输出及退出状态分别在 transaction 的 `root-module-final-record.json` 与 `root-memory-boundary-final-record.json`。实际 ONNX、真实 provider、GUI 与冷重启由 Root 独立验收；汇总见 [PA-02 实施记录](pa02-personal-memory.md)，不以受控 port 代替真实组件证据。初次未运行阶段的 manifest 与记录保留，最终源码包另存为新快照，不覆盖早先冻结证据。
