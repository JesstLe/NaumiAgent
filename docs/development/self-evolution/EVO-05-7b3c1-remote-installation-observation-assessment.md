# EVO-05.7b3c1 Remote Installation Observation Assessment

## 1. 目标与依赖

本切片在成功推广路径上建立 **Control Plane 单安装长期观察结论**。它只读取
[EVO-05.7b3b2b](EVO-05-7b3b2b-authenticated-observation-revision-http-transport.md) 已通过 mTLS 接收、并由
[EVO-05.7b3b1](EVO-05-7b3b1-signed-observation-revision-delivery.md) 逐 revision 验签的 durable ledger，再按
[EVO-05.7b1](EVO-05-7b1-stable-promotion-observation-contract.md) 冻结的时间、样本、phase 和 gap 规则计算一个 installation
verdict。

本切片有意先于 `EVO-05.7b3c2 Population Observation Aggregation`：单安装结论不能知道 Population denominator、成员缺失、
coverage 或 fleet health，因此不得直接签发 promoted Outcome。

## 2. 已实现模块

### 2.1 Content-addressed Assessment

`stable_promotion_installation_observation_assessments.py` 持久化完整、可重算的证据包：

- exact Observation Contract 与 Runtime Admission；
- 从 sequence 1 开始的全部 remote signed revision batches 及逐 batch Control Plane Receipt；
- ledger head revision/receipt、首尾观测时间、样本数、连续 operational suffix、窗口时长、最大间隔和最新样本年龄；
- `insufficient / passing / breached / censored` 四态结论及机械原因码；
- SHA-256 内容身份和 deterministic Assessment ID。

模型反序列化时会重新执行完整投影，篡改状态、指标、lineage、authority 或内容身份都会失败。

### 2.2 四态机械判定

判定优先级固定为：

1. `breached`：任一 `failed` 样本、任一 heartbeat gap 大于 Admission timeout，或非终态 head 超过 timeout；
2. `censored`：没有 breach，且 head 为 Contract 声明的 `draining / stopped`；
3. `insufficient`：没有 breach/censor，但 operational 样本少于 12 或连续 operational suffix 少于 3600 秒；
4. `passing`：上述门槛全部满足。

样本必须逐项绑定 exact Admission、Contract、installation member、runtime binding、subject、instance、epoch 和 startup origin；
sequence、revision hash、observation hash、batch remote head 与 Receipt binding 必须连续。评估时间不得早于 ledger head。

### 2.3 Durable source 与时间撤权

Store 与 Contract、Admission、Revision Store 共用 session SQLite，并在 `BEGIN IMMEDIATE` 内重新核对三类完整 durable source 后才写入。
相同内容幂等返回；相同 ID 不同内容、source 改变或损坏 row 均拒绝。

读取时重新验证：

- Assessment row 仍是 exact durable source；
- 它仍是该 Admission 的 latest Assessment；
- Contract、Admission 和远端 revision authority 仍 current；
- remote ledger 与 Assessment 内的完整 batches 完全相同；
- 非终态 `insufficient / passing` Receipt 尚未跨过 `last_observed_at + timeout`。

因此，新 revision 会立刻撤销旧结论；insufficient/passing Receipt 超时后只显示 `assessment_expired`，不会把未持久化的瞬时重算冒充 durable
告警。用户或 Agent 必须重新执行 `assess`，持久化 heartbeat-stale breach 后才能获得 installation alert authority。

### 2.4 双通道与 Engine 装配

Agent Tool：

`evolution_stable_promotion_installation_observation_assessment`

- `assess <admission-id>`：重算并持久化最新单安装结论；
- `inspect <admission-id>`：动态重验最近一次 durable Receipt。

用户命令与 Tool 使用同一 Service：

```text
/evolution stable-promotion-installation-observation assess <runtime-admission-id>
/evolution stable-promotion-installation-observation inspect <runtime-admission-id>
```

Engine 自动装配 Store/Service；PermissionChecker 将其归入 `evolution_evaluation_artifact`，所有模式均无需二次确认，bypass
保持直接通过。

## 3. Authority 边界

本切片可以授予：

- `installation_long_term_health_authority`：current、未过期且 passing 的 durable Assessment；
- `installation_health_alert_authority`：current 且 breached 的 durable Assessment。

以下字段在模型层固定为 `false`：

- `population_observation_authority`；
- `promoted_outcome_authority`；
- `learning_authority`；
- `promotion_authority`；
- `execution_authority`。

`insufficient` 和 `censored` 都是事实结论，但不授予健康或告警 authority。

## 4. 验收证据

定向真实链路测试：

```text
uv run pytest -q \
  tests/unit/test_evolution_stable_promotion_installation_observation_assessments.py -x
```

测试使用真实 release/deployment/runtime fixture、真实 HAR SQLite heartbeat、真实 installation key 签名、Control Plane revision
接收和 Assessment Store；没有缩短一小时 Contract，也没有 mock 长期指标。覆盖：

- 两个 Service 并发写同一 Assessment 的幂等收口；
- 初始 `insufficient`；
- 126+ 条真实 heartbeat 与多批 64 KiB revision 完整交付后的 `passing`；
- 新 ledger 使旧 Receipt stale；
- passing timeout 后先撤权、重新 assess 后形成 `heartbeat_stale` breach；
- 新 draining revision 使旧告警 stale，并形成 `censored`；
- Tool、Slash、moderate/bypass 无确认；
- Population authority 伪造与 SQLite JSON 篡改拒绝。

当前结果：完整小模块文件 `2 passed in 85.19s`。按项目约定未运行全量测试。

## 5. 尚未完成

下一独立切片为 `EVO-05.7b3c2 Population Observation Aggregation`，至少需要：

- 从 current Population Finalization 冻结 denominator 和 exact member set；
- 对每个 member 选择 current Admission/Assessment，并区分 missing、insufficient、passing、breached、censored；
- 计算 member coverage、duration coverage、缺失成员和 breach guardrail；
- 对所有输入做动态撤权和 content-addressed Population Receipt；
- 仍不直接复用单安装 health 作为 promoted Outcome；7b4 才负责 promoted/superseded ledger。
