# EVO-05.6b1 Immutable Rollback Source

## 目标

把 EVO-05.6a 的 exact Rollback Request 转换为可由后续执行器消费的不可变 baseline bytes。该切片只解决
“回滚内容从哪里来、如何证明未被替换”，不向未部署的 workspace 假装执行回滚。

## 为什么是必要前置

当前 local canary 在 immutable GREEN source 与 ARC-04 sandbox Worker 中运行，尚未切换真实发行 channel。直接覆盖
workspace 会回滚一个从未部署过的对象，也无法满足 ARC-07 的跨平台原子更新约束。因此依赖顺序固定为：

`Rollback Request → Immutable Rollback Source → ARC-07.5 version slot → Rollback Executor → Outcome`。

## 机械证据链

1. 重读 persisted Request，并确认属于 canonical workspace；
2. 要求 Request 绑定的 HMAC pause event 仍是 current kill-switch 状态；
3. 用 `git rev-parse <commit>^{commit}` 精确解析 baseline commit；
4. 对完整 `git ls-tree -r -z --full-tree` 重算 tree SHA-256；
5. `restore_baseline_blob` 路径必须存在为 100644/100755 blob，读取内容后匹配计划内 SHA-256；
6. `remove_created_file` 路径必须在 baseline 中不存在；
7. baseline bytes 写入只读 content-addressed storage，artifact 仅保存 path/digest/size/mode，不保存源码正文。

单文件上限 2 MiB、文件总量上限 32 MiB、receipt 上限 512 KiB；路径拒绝 absolute、`..`、反斜杠、NUL 和换行。
空文件是合法 restore blob。相同 digest 可物理去重，但总量按逻辑文件大小计费。

## 权限边界

Source 固定 `rollback_execution_authority=false`、`workspace_write_executed=false`、`git_write_executed=false`。
`data_restore_required=true` 时仍固定 `data_restore_input_ready=false`，直到 ARC-07.6 提供 exact data snapshot。冻结 Source
不能绕过恢复后的 pause，也不能代表已有可切换版本槽。

## 验收结果

- 真实临时 Git 仓库同时覆盖 modify restore 与 created-file remove；
- baseline tree、blob bytes、executable mode 与计划机械绑定；
- operator resume 后不能新建 Source；
- content-addressed blob 被篡改后重读失败；
- 空 baseline 文件可正确冻结；
- Engine 和公共 lazy exports 已接线；
- 只运行相关小模块测试，未运行全量测试。

## 下一切片

[ARC-07.5a](../architecture/ARC-07-5a-installed-version-slots.md) 已建立可验证的 installed-version slot、真实 boot
receipt 与原子 active pointer，[ARC-07.5b](../architecture/ARC-07-5b-stable-slot-launcher.md) 已让稳定 launcher
消费 pointer，[ARC-07.4a](../architecture/ARC-07-4a-exact-source-provenance.md) 已使 slot commit/tree 可与本 Source
机械比对。EVO-05.6b2 下一步只对 exact previous slot 签发 fenced rollback execution，并验证切换后的 Launch Resolution。
