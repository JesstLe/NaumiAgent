# HAR-09.2a Candidate Aggregation View v1

## 目标

从已验证的 Evolution Candidate Evidence 确定性计算次数、时间窗、趋势、来源及
Provider/Model/Platform 分布，并给出少量代表 Evidence。聚合器是纯函数，不创建第二套表，不修改
Candidate，也不依赖当前系统时间。

## 时间语义

- `anchor_at` 固定为 Candidate 的 `last_observed_at`；同一 Candidate 重复打开得到相同结果。
- 统计 `(anchor-window, anchor]` 内的 24 小时、7 天和 30 天 Evidence 数量。
- 趋势比较最近 7 天与此前 7 天两个等长窗口。
- 单条 Evidence 标记 `new`；总数少于 4 或跨度不足 7 天标记 `insufficient`。
- 当前窗口至少为 2 且达到前一窗口 1.5 倍时为 `increasing`；反向为 `decreasing`；其余为
  `stable`。

趋势只描述被观测频次，不表示缺陷严重度、修复优先级或统计显著性。

## 维度与边界

- source/provider/model/platform 分别计数，按 count 降序、value 字典序稳定输出。
- 空维度明确计为 `unknown`，不静默丢失分母。
- 每个维度最多输出 20 项，百分比按全部唯一 Evidence 计算并保留 1 位小数。
- 单次遍历最多处理 Candidate 契约允许的 10,000 条 Evidence。
- 代表 Evidence 包含首条、末条及各 source 的最新一条，按 Evidence ID 去重，最多 16 条。
- 代表项只含内部 URI、Evidence ID、时间和 SHA-256 前 12 位，不复制原始反馈、stdout 或源码。

## 接入

- `/evolution detail` 的共享 Markdown renderer 显示 trend、24h/7d/30d、前一 7d 和维度分布。
- `evolution/review` typed detail 携带同一 `candidate-aggregation-v1` 对象。
- 默认新 UI 用红/绿/青/黄分别表达上升、下降、稳定和数据不足，同时保留中文标签。
- TUI/legacy CLI 通过共享 Review Service 获得相同聚合事实。

## 验收

- 跨 14 天的真实 Feedback Evidence 能机械得到 increasing/decreasing。
- 单条 Evidence 为 new，短跨度重复为 insufficient。
- provider/model/platform/source 的 count、percentage、unknown 和稳定排序正确。
- 单条 Candidate 的 representative 不重复；secret 不进入聚合对象或 UI payload。
- 真实 Store→Review Service→Bridge→Node normalizer/page 链路读取前后 audit chain 不变。

## 非目标

本切片不跨 Candidate 合并 scope，不给出 Prioritization 分数，不做显著性检验，不实现 cooldown、
Proposal、approve/reject/defer 或 outcome tracking。
