# CC-01.2a License Scope Boundary

## 目标与边界

CC-01.1a/1b 能证明 source 身份和许可证证据文件是否变化，但 README 中存在一句许可证声明，不等于已经
证明每个源码路径可以复制、改编和随闭源产品再分发。本切片建立独立、严格、可机械验证的许可证范围
合同，把“证据存在”升级为“哪些路径允许哪些 intake mode”。

它不是法律意见，也不自动批准许可证。当前 Claude source checkout 没有独立 `LICENSE` 文件或标准 SPDX
标识，因此已提交的实际 scope 保持 `restricted_pending_legal_review`：只允许 `reference` 与
`reimplement`，不允许 `copy`、`adapt` 或据此声明再分发权。

## 权威合同

`frontend/terminal-ui/cc-license-scope.v1.json` 绑定：

- CC-01.1a source name、完整 commit 和去观察时间后的 identity SHA-256；
- license evidence 相对路径、文件 SHA-256 和 claim SHA-256，不复制 README 全文；
- `LicenseRef-Claude-Code-README` 表达式、审查状态、再分发状态、是否仍需法律复核；
- 按 source path prefix 排序、以最长匹配消歧的规则、允许的 intake mode、notice 状态和理由；
- 显式 exclusion 与 `unmatched_policy=reject`。

所有路径必须是规范化 POSIX 相对路径，不能含绝对路径、`..`、反斜线或控制字符。审查者、理由和规则
文本有长度、控制字符与 secret-shaped marker 边界；manifest 不保存 source 正文、Git diff、用户配置、
凭据或私有法律笔记。

## 机械规则

`verify_license_scope()` 先调用 CC-01.1a 的 live identity 审计，再执行：

1. identity invalid → scope `invalid`；identity/commit/license 变化 → scope `stale`；
2. scope 的 identity、commit、license path/digest/claim digest 必须逐项匹配；
3. rule、exclusion、license 和 notice 路径必须存在于 checkout 内，symlink 不能逃逸；
4. 以 legacy map 中每个 `claude_code` 路径为全集，限制 area/path 数量；空映射或空路径集失败关闭；
5. 使用最长 path prefix 计算每个映射路径的有效规则；missing/excluded/unmatched 任一出现即 fail closed；
6. 只有全部映射路径 covered 才返回 `valid`，并给出所有路径共同允许的 intake mode。

待法律复核状态在 Pydantic 模型层禁止 `copy/adapt` 和 `redistribution_status=allowed`；不是依靠调用者自律。
若未来人工批准 copy/adapt，必须明确再分发许可，并在规则要求 notice 时绑定真实 notice 文件。

## 确定性审计回执

`SourceLicenseScopeAudit` schema v1 包含：

- 自校验 `audit_id`、scope SHA-256、source identity SHA-256；
- `valid/stale/invalid`、review status 和排序去重的低基数 finding codes；
- mapped/covered/blocked 数量、共同允许模式；
- 每个映射路径的 covered/excluded/missing/unmatched、命中规则和允许模式。

回执不含观察时间；同一事实重复审计得到相同 ID。字段篡改、重复路径、非法 finding code、计数漂移、
valid+finding、stale+coverage 等矛盾组合均被严格模型拒绝。

## 维护者命令

```bash
python3 -m naumi_agent.claude_source.license_scope \
  --scope frontend/terminal-ui/cc-license-scope.v1.json \
  --identity frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root .
```

命令只读 source 和项目 mapping，只打印结构化 JSON，不修改 checkout、manifest、用户状态库或正式审批
历史。输入损坏时返回稳定 invalid 错误，不尝试猜测许可证。

## 验收证据

- 临时真实 Git checkout 覆盖确定性 valid、source stale、scope binding stale；
- 待法律复核 scope 不能声明 copy/adapt 或允许再分发；
- mapping 中 missing、unmatched、excluded 路径均 fail closed；
- symlink 逃逸、非法相对路径、secret-shaped 理由与额外字段被拒绝；
- manifest 原子读写、audit digest/计数篡改和 CLI 只读行为均有小模块测试；
- 当前真实 Claude source `b9c3fb6c...` 审计为 scope `valid`、review
  `restricted_pending_legal_review`，28/28 路径覆盖，共同允许模式仅 `reference/reimplement`。

## 明确未完成

- 人工法律复核和任何 copy/adapt 批准尚未发生；`valid` 只证明受限政策自洽且覆盖完整。
- scope 当前由版本库 code review 治理，尚未接入 CC-01.1b 的 append-only 审批历史；后续变更不能仅凭
  文件被修改就视为人工法律批准。
- CC-01.3 仍需把每个 mapping item 升级为 v2，显式引用 effective scope rule 与 intake decision。
- CC-01.4/1.5/1.6 的 classifier、provenance 和完整 review gate 尚未实现。
- CC-05.1 当前 receipt 仍保持 legacy schema；后续必须以兼容 variant 引用 scope audit，不能静默改变 v1。
- Brainless 是另一 source identity。其 MIT 声明不能借用本 Claude scope；未来复制组件前必须单独建立
  identity、license scope 和 provenance。
