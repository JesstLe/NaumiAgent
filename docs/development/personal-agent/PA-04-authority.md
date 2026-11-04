# PA-04 周期来源与执行授权

日期：2026-11-05。

本模块实现 `RoutineAuthorityMixin`，不拥有另一个执行、结果或已读状态机。来源和原授权以
历史 Routine、原 occurrence、Item 创建回执、prepared intent、Artifact 版本与真实 AgentJob
为权威。`artifacts.py / artifact_store.py / items.py` 的接线由 Root 负责，本模块不编辑它们。

## 1. 读取来源与执行授权分离

- `authorize_definition_sources(definition, reader_actor=None)` 始终按原 assignee 检查显式输入。
- 正文的 `source_context` 使用原 exact artifact refs、memory refs 与原 memory item scope。
  人工读取 `None` 使用原 assignee；模型读取还需要当前 reader 自己通过同一 exact 来源授权。
- 不把 `main` 的计划管理身份变成资料或记忆超级读取身份，不替换 grant id/revision。
- `authorize_execution_definition(definition)` 单独检查当前 Agent 版本与 Item 原执行策略。
  因而历史报告仍可读取，不会因父计划暂停、结束、换 epoch 或 Agent 新修订而消失；原来源撤权
  或修正仍会阻止其正文继续返回。
- 来源对话 id 保留为不可变 provenance，不把它们当作新的对话读取授权，也不采样该对话后来
  增加的来源来改写已冻结定义。

## 2. typed Artifact 来源

`authorize_artifact(snapshot, actor_id)` 递归检查 immutable ancestors。祖先通过私有资料服务的
`_call('get', actor_id=..., artifact_id=..., version=...)` 原始端口读取，避免公共 `get()` 再次
进入同一 hook。来源的 exact SHA、原历史修订、原 assignee、workspace、epoch、occurrence
逐项绑定；不以当前新定义替代历史定义。

两类 typed evidence：

1. `routine_plan_refs`：routine id、definition revision/digest、原 assignee、workspace。
2. `routine_refs`：以上加原 epoch 与真实 occurrence id。

跨 Artifact 与定义的授权递归共享同一请求级 walk，检测自依赖环并限制最多 32 层、1024 个
校验节点。不同请求不复用旧授权缓存。Root 的 ArtifactStore 为带上述 evidence 的版本设置
`authority_version=3`，旧源码拒绝该版本，而不是忽略未知来源并放行。

## 3. 模型正文读取必须实际 pin

`pin_plan_read(public_routine, actor)` 对人工 `None` 保持原样。模型入口必须具有当前、持久化、
归属正确且仍有效的 Session；main 只能管理其自己发起的计划。方法核对返回正文与确切历史
定义，发布实际 UTF-8 `.txt` material，保存 `routine_plan_refs` 与原 source context，再把确切
Artifact 引用绑定当前 Session，最后重新核验 actor、Session 与来源后才返回正文。

稳定请求身份绑定原 plan ref、reader 与实际返回正文摘要；相同读取重放复用原 material。
绑定不自动把当前对话的所有 refs 加进 Artifact，避免把此前 pin 的自己变成自依赖来源。
列表只返回 opaque 摘要、preview Tool 只返回 ID/digest/slot 的协议由 Root 接入层保持。

## 4. 每次发生的真实 guard

`create_occurrence_guard(occurrence, routine_snapshot)` 要求原发生账本与有效预算预留真实存在，
并核对原历史定义。存在显式输入时读取第一份原文件的实际字节，创建同字节、同类型的 material
wrapper，保留第一份 exact source ref 和正文 source refs；wrapper 替换原第一 ref，不增加输入数。
其余显式输入保持原 exact refs。

没有显式输入时创建实际 `.txt` guard，正文是该真实发生与完整原 source context。guard 自身
占一份输入；目录内容仍由 Item `capture_inputs()` 真实枚举，guard 加 64 份目录文件必须报
65 份超过原 64 容量，不能截断或声称已完整读取。来源 ancestry 合并超过 64 同样明确拒绝。

Root 在调用 `Item.create` 前持久保存 `guard_ref / child_input_refs / item_request_id` 和原创建
尝试状态。所有 guard evidence 绑定实际 reserved occurrence，不创建一份只带 prompt 的“授权”。

## 5. 执行 gate

`authorize_occurrence_execution(occurrence, intent)`：

1. 从真实账本和原历史定义重读 occurrence binding，并核对原预算预留。
2. 从原 ItemStore 重读 prepared intent，不信任调用者自行拼接的 request 或 references。
3. 沿原 `item_request_id` 查询 `find_request`，核对 `operation=create`、原 committed Item、
   once key、定义摘要、Actor、workspace、正文、目录、输入与冻结策略。未回填 Item id 时仍
   沿同一原回执核验，回执缺失保持待核对，不创建新 Item。
4. guard 必须保留确切原 source context、第一份字节摘要、真实 occurrence evidence；执行策略
   不能扩大原授权，单次费用只允许降低到原实际 reservation 内。
5. 当前 Agent 版本、工具、策略及原来源再次通过；真实 Job request 必须完全一致。
6. 父计划 `cancelled/closed` 在下一边界始终阻断；`paused` 或新 epoch 仅阻断尚未开始的执行。
   原 Job 只有真实 `RUNNING`、同一 request、有效 holder/epoch、租约未过期才可继续完成。
   不把 UI 对 CLAIMED 的 running 映射当作已启动，不把 UNKNOWN/原 Job 缺失当作可重发。

## 6. 基线与验证记录

事务目录：`.naumi/transactions/pa04-recurring-20261105/authority/`。

在 Root 封存的 bd71c2f0 原源码下真实执行 `baseline_probe.py`，字面结果为：

```text
AUTHORITY_MODULE_ABSENT True
LEGACY_ACCEPTED_UNVERIFIABLE_ROUTINE_EVIDENCE True
LEGACY_READ b'private routine body'
EXIT_STATUS: 0
```

该反例使用真实临时 ArtifactStore：旧模块没有 hook，并接受无法核验的 routine evidence 后
读回实际正文；没有以 mock 的“拒绝”当作新授权已存在。本模块集中测试使用真实 SQLite
Routine/Artifact/Memory/Item/AgentJob，模型和语义向量 transport 不启动。

完整模块集中回归命令与字面结果：

```text
.venv/Scripts/python.exe -m pytest tests/unit/test_personal_routine_authority.py tests/unit/test_personal_artifact_store.py tests/unit/test_personal_memory_runtime.py tests/unit/test_personal_item_store.py -q --tb=short
201 passed, 2 skipped in 59.12s
EXIT_STATUS: 0

.venv/Scripts/python.exe -m ruff check src/naumi_agent/personal/routine_authority.py tests/unit/test_personal_routine_authority.py
All checks passed!
EXIT_STATUS: 0
```

其中本模块 49 项全部执行；2 项相邻旧测试 skip 不计作通过。覆盖真实 64 份输入的 wrapper
替换、真实目录 64 文件加 guard 的超限、原 memory correction/reader/范围、原 grant 不替换、
Session/Agent 在 pin 中途变化、父状态与真实 Job CLAIMED/RUNNING/租约、未回填 Item id、
原 request/intent、epoch 和 typed refs 的全部 binding。首轮夹具抽象 Tool name 漏声明、随后
夹具缺 boundary/重复 event id 和一次 Root 错误码期望不符的原日志均保留；没有放宽真实协议。

独立 probe 命令与结果：

```text
.venv/Scripts/python.exe .naumi/transactions/pa04-recurring-20261105/authority/authority_probe.py
ORIGINAL_ITEM_RECEIPT_WITHOUT_BACKFILL OK
WRAPPER_ACTUAL_BYTES_AND_AUTHORITY3 OK
REAL_RUNNING_PAUSE_AND_NEW_EPOCH OK
PARENT_CANCEL_BLOCKED occurrence_cancelled
HISTORICAL_SOURCES_AFTER_PARENT_END OK
PLAN_BODY_REAL_MATERIAL_AND_SESSION_PIN OK
ORIGINAL_MEMORY_CORRECTION_BLOCKED memory_access_revoked
AUTHORITY_PROBE_OK
EXIT_STATUS: 0
```

`authority-samples.json` 保存实际创建的 Routine、发生、prepared intent、guard ref 和实际 Artifact
目录，随后以 Root 原封存源码读取同一个真实 authority3 文件，结果为：

```text
PYTHONPATH=original/src .venv/Scripts/python.exe .naumi/transactions/pa04-recurring-20261105/authority/old_reader_probe.py
OLD_READER_REJECTED_AUTHORITY3 storage_error
EXIT_STATUS: 0
```

上述旧源码读同一真实新数据的反例证明回退不忽略 authority3；它不是另一份伪造“拒绝”数据。
仅本模块三个新增文件的副本 guarded rollback、后续编辑零写入保护、源码摘要和四个可重开产物
见事务目录 `VERIFICATION.txt`。Root 接入层、全产品真实执行与长期运行由各自验收记录收口。
