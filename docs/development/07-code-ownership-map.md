# 模块代码归属与测试映射

下表是领取模块时的首选落点，不等于授权修改全部列出文件。实现模型必须先验证真实源码；新增
文件优先放在相应领域目录，禁止把业务继续堆进 `main.py` 或 `engine.py`。

## Harness

| ID | 建议生产落点 | 定向测试 |
| --- | --- | --- |
| HAR-05 | `harness/replay*.py`, `service.py`, `tools.py`, slash router | `test_harness_replay*.py`, surfaces |
| HAR-06 | `harness/retention.py`, `artifact_gc.py`, Session reconciliation | retention、artifact safety、session deletion integration |
| HAR-07 | `ui/messages`, `ui/bridge.py`, terminal/TUI receipt components | bridge、receipt、resume process tests |
| HAR-08 | `harness/eval*.py`, Store migration, eval tools | eval schema/runner/baseline/integration |
| HAR-09 | `harness/feedback*.py`, Workbench proposal adapter | fingerprint/policy/review/outcome |
| HAR-10 | `harness/orchestration*.py`, Pursuit adapter | lease/heartbeat/queue/recovery/soak |

## CLI/TUI/New UI

| ID | 建议生产落点 | 定向测试 |
| --- | --- | --- |
| UI-10 | `ui/workbench_page.py`, Bridge, terminal components, TUI | workbench snapshot/action/process |
| UI-11 | `ui/task_panel.py`, terminal task component/state | task navigation/viewport/cancel |
| UI-12 | `ui/permission_panel.py`, Bridge, permission components | permission concurrency/recovery |
| UI-13 | `ui/doctor.py`, debug trace/viewer components | doctor probes/export/process cleanup |
| UI-14 | keybindings/completer/input-buffer/QuickOpen modules | IME/key sequence/fuzzy/conflict |
| UI-15 | render cache/history/scroll/render benchmark | 10k timeline/token burst/resize |
| UI-16 | terminal capabilities/ANSI/theme/platform launcher | terminal matrix/no-color/Windows |
| UI-17 | protocol capability manifest/release scripts | parity/golden/install/upgrade |

## Claude Code 对齐

| ID | 建议生产落点 | 定向测试 |
| --- | --- | --- |
| CC-01 | `cc-source-map.v2.json`, schema/validator/audit script | path/license/stale/provenance |
| CC-02 | 独立 `frontend/terminal-ui-ink-spike/` | shared fixtures + benchmark；不改默认入口 |
| CC-03 | terminal components + UI view model adapters | source behavior/golden/Naumi divergence |
| CC-04 | config/plugin/skill/MCP manifest 与 trust service | discovery/conflict/install/isolation |
| CC-05 | source diff/report tooling | no-change/deleted-path/license change |

## Future Architecture

| ID | 建议生产落点 | 定向测试 |
| --- | --- | --- |
| ARC-01 | 新 `core/ports`, `runtime/ports`, composition root | import graph/rules/port contracts |
| ARC-02 | `runtime/service`, transport/client/supervisor | socket/pipe/reconnect/multi-client |
| ARC-03 | `protocol/schemas`, Python/TS validators | conformance/version/gap fixtures |
| ARC-04 | `daemons/` 与 Runtime worker adapters | grant/idempotency/cancel/crash |
| ARC-05 | `persistence/store_catalog.py`, 后续 migration/recovery/retention | historical fixtures/rollback/saga |
| ARC-06 | `runtime/scheduler`, admission/backpressure | load/fairness/429/isolation/soak |
| ARC-07 | build/release/update scripts 与 manifests | unpack/sign/install/upgrade/rollback |
| ARC-08 | observability/SLO/fault injection/runbooks | redaction/metrics/backup/disaster |

## Self-Evolution

| ID | 建议生产落点 | 定向测试 |
| --- | --- | --- |
| EVO-01 | `evolution/candidates.py`, adapters/store/tools | dedup/eligibility/ranking/privacy |
| EVO-02 | `evolution/experiments.py`, Worktree adapter | dirty-tree/isolation/escape/crash |
| EVO-03 | `evolution/evaluator.py`, Harness Eval adapter | baseline/candidate/flaky/guardrail |
| EVO-04 | `evolution/reflection.py`, reward-hack rules | mechanical veto/counterfactual/escalate |
| EVO-05 | `evolution/promotion.py`, release adapter | approval/rebase/canary/rollback |
| EVO-06 | `evolution/capabilities.py`, shadow registry | shadow/limited activation/retirement |

## 禁止越界

- UI 模块不得修改 Tool execute 语义或 PermissionChecker 规则。
- Harness 模块不得复制 Session/Task/Pursuit Store。
- CC 模块不得直接取得 Python Runtime 权威。
- Evolution 模块不得修改 protected safety/update/migration 基线。
- Architecture 模块不得以移动目录代替端口和契约验证。
