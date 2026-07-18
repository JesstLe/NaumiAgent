# EVO-02.4b 多文件 Scope 签发协议

## 目标

让 EVO-02.5c 多文件 write-set 能从真实 Feedback→Candidate→Proposal→Contract→Mutation Plan 链获得
权威文件集合，而不是在测试或 Writer 内伪造第二个路径。

## 显式语法

多文件范围使用：

```text
files:path/to/first.py,path/to/second.py
```

规则：

- 必须显式以 `files:` 开头，包含 2..16 个文件；
- 逗号只作为文件分隔符，路径本身不得含 `:` 或符号级后缀；
- 保留用户给定顺序，作为后续 Plan/write-set 的确定性顺序；
- 拒绝空项、重复、绝对路径、`..`、尾随 `/`、控制字符和 Windows drive path；
- 单文件历史语法 `path/to/file.py:symbol` 保持不变。

多文件 Proposal 当前统一分类为 `code/scope_prefix:files`。跨 tool/profile/test 域的自动细分类不是本切片
目标；文件权限仍由 Static Guard protected/dependency policy 决定，分类不会扩大权限。

## 权威传播

1. `FeedbackObservation` 在进入 Candidate Store 前验证显式语法的数量、重复和路径安全；
2. `parse_proposal_scope_files()` 是 Proposal 层的规范解析器；
3. `EvolutionProposalPreview.intended_files` 保存解析后的有序 tuple；
4. `ExperimentScope` 重新解析 `impact_scope`，要求结果与 `allowed_files` 完全一致；
5. Contract 风险预算可以大于本次文件数，但不能改变 `allowed_files`；
6. `EvolutionMutationPlanner` 扫描全部真实 baseline blob，并把 `max_changed_files` 收紧到授权文件数；
7. Static Guard 要求 proposed content 集合与 Plan scope 一致并分别计算 change fact。

因此展示范围、Contract 权限和最终 Plan 不存在三套可漂移的文件列表。

## 验收证据

- 相同 Candidate revision 两次生成完全相同的双文件 Proposal；
- malformed、重复、17 文件、绝对/穿越/符号后缀在解析或 intake 前被拒绝；
- 手工构造 `impact_scope` 与 `allowed_files` 顺序/内容不一致时 Pydantic 拒绝；
- 真实 approved Workbench Proposal 签发双文件 Contract；
- 真实 Lease→Snapshot→Mutation Plan 得到两个 baseline fact，Plan 文件预算收紧为 2；
- 同一双文件 proposed content 通过 Static Guard，未发生任何文件写入。

## 当前不足与后续

- 语法当前不支持含逗号的仓库文件名；这是为了保持 CLI/TUI 输入可读和无转义歧义；
- 一条多文件 Feedback 只表达一个共同 hypothesis/metric，不支持逐文件不同 rationale；后续 Mutation
  Receipt 可在 change fact 层补充逐文件理由，但不能改变 approved scope；
- 本切片只签发多文件权限，不实现写入、事务或回滚。

下一步进入 EVO-02.5c：一次 Guard Receipt 对应一个持久 write-set transaction，逐文件落盘并在失败或
重启时严格逆序恢复。
