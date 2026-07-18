# EVO-02.1a 不可执行 Experiment Contract v1

## 目标

把一个已由人类批准、来源仍可验证的 Evolution Workbench Proposal 转换为确定性实验 manifest。
该 manifest 固化 Candidate/Proposal provenance、真实 Git baseline、允许文件、预算、允许工具与机械检查，
但在 worktree lease、source snapshot 和 static guard 完成前固定 `execution_ready=false`。

本切片是 EVO-02 的安全前置，不创建 worktree、不调用模型、不写 patch、不执行检查、不安装依赖，
也不把 approved 等同于 experiment eligible。

## 签发前置

`EvolutionExperimentContractIssuer` 必须同时验证：

1. Workbench Proposal 属于当前 session 且状态为 `approved`；
2. 来源为 `evolution_candidate`，并有 reviewer、decision time 与 `proposal-governance-v1`；
3. Candidate Store 当前 revision 仍能生成可信 Preview；
4. Candidate ID/revision/digest、Preview ID、generator、kind、risk、impact scope、目标文件与验证绑定均与
   已批准 Proposal 完全一致；
5. workspace 是精确 Git repository root，HEAD 是完整 40/64 位 object ID；
6. 文件数量和自定义预算没有超过 risk 对应的 v1 上限。

任一条件不满足都 fail-closed。Candidate 在批准后产生新 revision 时，旧批准不能自动授权新内容；必须
重新进入 Proposal 治理。

## Manifest

- `manifest_sha256`：排除 identity 字段后的 canonical JSON 完整 SHA-256；`contract_id` 是 `evx_` 加
  该 digest 前 24 位。相同可信输入、baseline、seed 与预算得到相同 identity，任一字段被篡改后模型
  校验失败。
- `source`：session/mission/task、Workbench Proposal、Preview、Candidate revision/digest、reviewer 与
  approval time。
- `baseline`：精确仓库 HEAD 和签发时 dirty 布尔值。dirty 只记录事实，不读取或覆盖用户改动。
- `scope`：最多 16 个已批准安全相对路径；绝对路径、`..`、控制字符和重复路径拒绝。
- `budget`：文件数、变更行数、工具调用、时长、尝试次数；自定义值只能收紧，不能突破 risk cap。
- `allowed_tools`：固定最小集合 `file_read/glob/grep/file_edit/file_write`；不含 shell、网络和安装器。
- `allowed_checks`：来自可信 Preview 的 Harness replay、静态自审或反馈复发指标，不接受任意命令文本。
- `network_access=false`、`dependency_installation=false`。
- `requires_worktree_lease/source_snapshot/static_guard=true`，`execution_ready=false`。

## Risk Budget v1

| risk | files | changed lines | tool calls | seconds | attempts |
| --- | ---: | ---: | ---: | ---: | ---: |
| low | 8 | 800 | 80 | 1800 | 3 |
| medium | 6 | 500 | 60 | 1200 | 2 |
| high | 4 | 300 | 40 | 900 | 2 |
| critical | 2 | 150 | 25 | 600 | 1 |

预算是上限，不是目标；后续 planner 应进一步收紧。已批准文件数量超过对应 cap 时不签发。

## Git 读取边界

- 仅执行无 shell 的 `git -C <root> rev-parse/status`，每次 5 秒超时并禁用 optional locks；
- nested path 不会默认为父仓库，必须传入精确 repository root；
- Git 读取失败返回固定错误，不把 stderr、环境变量或路径细节写入 Contract；
- dirty workspace 的 tracked/untracked 内容不会进入 manifest，只有布尔事实。

## 验收证据

- 真实 Git 仓库 + Candidate SQLite + Workbench SQLite + Queue + human approve + Issuer 端到端签发；
- 同一输入两次签发完全相等，主工作树文件字节和 index 不变；
- open Proposal、Candidate revision 漂移、过大预算与 contract identity 篡改均拒绝；
- nested repository path 拒绝，clean/dirty baseline 可区分；
- Proposal Queue 改为复用同一 validation binding 函数，避免签发方复制字符串契约。

## 明确未包含

- Contract 持久化、列表、UI、斜杠命令或 Agent Tool；
- EVO-02.3 tree/config/tool source snapshot digest；
- EVO-02.4 planner、02.5 patch writer、02.6 完整 static guard、02.7 mutation receipt；
- EVO-03 验证执行和 HAR-09.6 outcome tracking。

EVO-02.2a 已实现 Contract 绑定的持久 worktree lease、崩溃恢复、过期回收和 dirty tombstone，详见
`EVO-02-2a-worktree-lease.md`。下一切片应实现 EVO-02.3a Source Snapshot，仍不运行 mutation planner。
