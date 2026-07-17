# UI-10.1 Workbench Snapshot 实施计划

1. RED：WorkbenchService 快照 revision、stream、counts、selection 与重复查询。
2. GREEN：在现有 Service 内加入有界 session 指纹状态，不新增持久化状态源。
3. RED：Python client protocol 与 Bridge 显式请求、会话隔离、失败脱敏。
4. GREEN：增加 `workbench/request` 和统一 Bridge 快照发送路径，替换任务执行中的裸快照。
5. RED：Node normalizer/reducer 对重复、乱序、stream 切换、session 串线和 `/workbench` 路由。
6. GREEN：加入 refresh action，并在终端 transport 中发送完整快照请求。
7. 真实场景：SQLite Task/Workbench Store → 新 Service → Bridge → Node reducer。
8. 定向 Ruff、compile、pytest、Node tests；更新 UI-10 文档，自审后独立提交并推送 main。
