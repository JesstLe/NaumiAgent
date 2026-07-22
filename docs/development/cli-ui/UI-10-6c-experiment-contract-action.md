# UI-10.6c Workbench Experiment Contract 动作

## 用户任务

用户批准一个 Evolution Proposal 后，需要在同一 Reviews 页明确看到它仍未执行，并能选择是否把已批准范围冻结为
不可执行 Experiment Contract。该动作必须在 New UI 与 Textual TUI 中一致，并由 Python authority 决定结果。

## 交互状态机

```text
approved Evolution Proposal
  └─ c
      ├─ bypass ──────────────> loading
      └─ normal -> confirm ───> loading
                                  ├─ completed -> Contract/Authority 回执
                                  └─ blocked/conflict/error -> 有界中文错误
```

- `c` 只对 `state=approved && source_kind=evolution_candidate` 生效。
- confirm 页明确说明不会修改代码、运行实验或批准发布。
- loading 阶段吞掉重复输入；取消不发送写请求。
- completed 后保留权威 Snapshot 中的 approved Proposal，便于幂等重开和查看 Contract 回执。
- bypass 直接发送 `confirmed=false`，Bridge 的权限层识别 bypass 后执行，不伪造确认。

## 协议与渲染

客户端继续发送 `workbench/proposal/action`，action 为 `issue_contract`。服务端结果继续使用
`workbench/proposal/action_result`，并在成功时增加严格的 `experiment_contract` 摘要。

New UI 在 80/120/200 列保持 Proposal 状态、`c` 提示、Contract ID、Authority ID 和
`execution_ready=false` 可见。Textual fallback 使用同一键位和权限规则；Markdown 特殊字符经过转义，
但用户看到的 identity 不丢失。

## Authority 边界

前端不生成 seed、Contract ID、budget 或 scope，也不直接写 SQLite。稳定 seed、单飞、历史迁移、Candidate
重验和 Authority 持久化全部由 HAR-09.5c/EVO-02.1b 后端承担。

完整后端契约、验证和未完成边界见
`../harness/HAR-09-5c-explicit-experiment-contract-issuance.md`。
