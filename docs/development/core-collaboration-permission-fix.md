# 协作大权限载荷修复（F10）

## 完成内容

协作原来将完整确认参数直接放进 64 KiB journal。合法的大文件正文会先触发日志大小异常，父确认回调和协作页面看不到请求。

本模块区分观察日志与完整批准载荷：

- `append_permission_request()` 检查完整参数；参数仍使用既有 `permission_arguments_sha256()` 的 **256 KiB** 契约。
- 完整 payload（包含会话、工具、理由等元数据）独立限定为 **512 KiB**，避免参数本身在合法边界时因附加元数据被拒绝。
- 小 payload 保持原 inline 事件格式，不需要读取密钥。
- 大 payload 使用已有 Runtime payload AES-GCM 原语，写入 `collaboration_permission_payloads` 专表；journal 仍小于 64 KiB，只存版本、完整载荷 digest 和字节数。
- AAD 绑定 workspace、collaboration、event key、step、session、call 与完整载荷 digest。读取重新验证事件 digest、引用字段、密文、完整摘要和执行会话归属，才恢复参数。
- 同 event key 的重复请求按完整明文 canonical digest 比较，真正幂等；不同正文冲突。密文和 journal 在同一个 SQLite 写事务落盘，失败不留下半条权限记录。
- 构造、建表、迁移、list/events 与小 inline 请求均不加载密钥。不隐式生成密钥，密钥错误没有明文 fallback。
- `confirm()` 仍将原始完整 payload 交给父 confirmer；`_view(include_events=True)` 恢复完整参数供现有协作 UI 展示。没有将摘要替换为待批准正文。
- 内部普通 events 读取保持有界，不为内部状态投影解密所有权限正文。权限观察不成为执行 authority；实际执行回执仍绑定原始完整参数。

## schema-1 与代码回滚兼容

采用 **schema-1 的增量建表**，不增加 `user_version`，不重写旧计划或事件。原版 store 会忽略新增表，继续读取计划和普通日志。

大 payload 的原始 journal 引用刻意不包含 `session_id` / `call_id` / `arguments`。旧版协作 UI 要求 session/call 才显示批准按钮，因此代码回滚后不会对未知引用提供盲目批准入口。新版完成校验后才恢复这些字段。

代码回滚意味着大权限请求应重新由支持该格式的版本读取，不会自动重放旧执行或自动批准。旧小请求和非权限路径仍可读。没有回滚/恢复用户数据库。

实际将 `6a8e2ef7` 的旧 `CollaborationStore` 载入进程，读取新版本产生的数据库、追加普通事件，再交还新版解密读取，结果：

```text
schema=1
old_plan_read=true
old_inline_permission_read=true
old_ui_has_no_blind_approval_ids=true
old_write_new_read=true
restored_new_code_full_payload=true
encrypted_row_preserved=true
database_restored=false
SCHEMA1_CODE_ROLLBACK_COMPATIBLE=True
ROLLBACK_COMPAT_EXIT=0
```

## 验证

基线两个核心行为测试失败，exit 1：缺失独立载荷 API；真实 child 工具调用的父回调未到达，日志原因为“协作事件内容最多允许 64 KB”。

修复后的四模块集中验证：

```text
147 passed in 31.45s
SUITE_EXIT=0
```

覆盖：

- 70,000 字节 ASCII 与 96,000 字节 UTF-8 中文参数。
- 真实 child Engine、PermissionChecker、FileWriteTool、临时 SQLite 与认证 HTTP detail：确认挂起时页面数据拥有完整参数；allow 后内容相同，deny 后无文件；child 持久回执摘要与完整 arguments 一致。
- 精确 256 KiB 参数加额外元数据仍可用；参数超过既有上限及完整 payload 超过 512 KiB 均拒绝。
- 重启解密恢复、detached返回、同key不同内容冲突、跨实例并发幂等。
- wrong workspace、错误key、密文/摘要/执行身份篡改、跨协作密文交换、载荷缺失均 fail closed。
- 无密钥时小事件和建表照常；大事件拒绝后无部分记录。
- journal插入失败时专表插入一并回滚。
- 新表缺失时schema1增量建立，旧inline事件保留，未来schema仍拒绝降级。
- 原有协作与store的121项测试保留通过。

`ruff check src/` 和新测试 lint 通过。使用主仓 `.venv` 并显式加载修复源码；测试 HOME、USERPROFILE、数据库及workspace隔离，没有真实模型或用户数据操作。

首轮修复后 136 通过、2 失败是测试从父 store 查找 child 回执，已改为核对真实 child store，生产权限逻辑未为测试改写。首次代码回滚证据脚本自身的 SQLite 查看连接未关闭造成临时目录清理错误，改成显式 closing 后复跑退出 0；初次记录保留。

## 自我审视与边界

没有提高journal限额掩盖载荷职责混淆；没有裁剪审批正文或让损坏/缺失载荷显示成可批准摘要。加密专表不承载AgentJob完成状态；回滚不擦除用户数据。测试验证实际工具、数据库与HTTP返回契约，但不是原生浏览器点击或外部模型端到端验收。

外部证据：`collaboration-payload-baseline.json/txt`、`collaboration-payload-final.json/txt`、`verify_permission_code_rollback.py`、`collaboration-code-rollback.txt`。
