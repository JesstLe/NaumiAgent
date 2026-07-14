# CC-01 源码采纳治理与映射更新

## 目标

把一次性审计升级为可重复 source intake：每个映射项都能证明来源版本、适用许可证、迁入
方式、行为差异和回归测试。

## 子模块

- CC-01.1 Source identity：repo path、remote、commit、dirty 状态、审计时间。
- CC-01.2 License evidence：README/LICENSE 路径、适用范围、不可复用区域。
- CC-01.3 Mapping schema：area/source paths/target paths/status/owner/tests/divergence。
- CC-01.4 Intake classifier：copy/adapt/reimplement/reference/reject 五种决定。
- CC-01.5 Provenance header：复制/改编文件的机器可检索来源注释或 manifest。
- CC-01.6 Review gate：安全、依赖、包体、维护成本和行为收益评估。

## 验收标准

- source map 每条 source/target 路径存在，缺失路径使检查失败。
- commit 变化时映射标记 stale，不继续声称“已审查”。
- dirty source 不作为稳定基线，除非记录 diff digest 和理由。
- copied/adapted 项有许可证证据和 provenance；reference/reimplement 不复制表达性代码。
- 自动检查不读取或提交源仓库 secret、构建产物和用户配置。
- 真实审计命令生成新 manifest，与现有 map 差异可复核。

## 产物

`cc-source-map.v2.json`、schema、validator、审计报告；v1 保留兼容读取一个发布周期。
