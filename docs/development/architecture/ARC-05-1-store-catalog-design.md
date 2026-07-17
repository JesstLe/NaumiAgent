# ARC-05.1 状态 Store Catalog 设计

## 问题与边界

NaumiAgent 的持久状态目前分散在共享 SQLite、独立 SQLite、JSON 文件和目录型存储中。
Session、Task、Workbench 共用 `sessions.db`，Harness 当前为 `user_version=5`，其他多处 Store
仍各自建表或完全没有 schema version。如果直接开始写 Migration Runner，会出现重复迁移同一
物理库、漏掉外部用户状态、或把尚未创建的惰性 Store 当成损坏等问题。

本轮只完成 ARC-05.1：建立权威物理 Store 目录和只读运行时探测，并接入共享 Doctor。
不执行迁移、不修改权限、不创建缺失 Store，也不把完整性检查或备份提前塞进本模块。

## Catalog 数据契约

每个物理 Store 必须声明：

- 稳定 `store_id`，不得以路径充当身份；
- 绝对路径和存储类型（SQLite、JSON、目录）；
- 一个或多个 owner；共享 `sessions.db` 只登记一次；
- version strategy 与当前程序支持的 schema version；
- 数据敏感级别和 retention policy；
- 是否惰性创建、用途说明。

首批目录覆盖：Runtime Core（Session/Task/Workbench）、Chat Runs、Goal、Pursuit、Scheduler、
Harness Evidence、Harness Trust、Background、Browser Runtime、Browser Daemon 和 Vector Memory。

## 只读探测

SQLite 必须通过 URI `mode=ro` 打开，读取 `PRAGMA user_version` 后立即关闭；缺失文件不得被
创建。JSON 只做有界语法/schema version 读取；目录只做类型、权限和存在性探测。探测结果
区分：`absent`、`ready`、`legacy_unversioned`、`upgrade_required`、
`unsupported_newer`、`corrupt`、`wrong_type`、`unreadable`。

`absent` 对惰性 Store 是正常状态；未版本化 Store 是迁移治理警告；高于当前支持版本、损坏、
类型错误或不可读是错误。POSIX 上目录不得向 group/other 开放，敏感文件不得有 group/other
权限；本轮只报告，不自动 chmod。Windows ACL 的等价修复留给 ARC-05.3/ARC-07。

## Doctor 表面

共享 Doctor 新增“状态存储目录”检查，显示登记数、已存在数、未创建数、警告和错误数，
并列出少量需要治理的稳定 Store ID。报告不输出文件内容，也不读取任何模型密钥。

## 验收

1. 默认目录无重复 ID/路径，且共享 `sessions.db` 的 owners 完整。
2. 对全空目录探测前后字节级文件树不变。
3. 当前 Harness schema、未版本化 SQLite、未来版本 SQLite、损坏 SQLite、错误 JSON 和
   POSIX 过宽权限都有独立测试。
4. Doctor 使用同一 Catalog 结果并提供中文建议。
5. 对本机真实配置执行只读探测，比较检查前后文件大小、mtime 和 SHA-256，证明没有写入。

## 后续依赖

ARC-05.2 Migration Runner 只能接受 Catalog 的稳定 `store_id`，并按物理路径去重；
ARC-05.3 Backup、ARC-05.4 Integrity、HAR-06 Reconciliation 和 HAR-08 Baseline Store 都必须
复用同一目录，不能再自行推导路径。
