# UI-10.2 Workbench Overview 实施计划

1. RED：组件 loading/empty/ready/error，真实字段映射及 80/120/200 列边界。
2. GREEN：新增纯渲染 `workbench-overview.js`，不查询 Store、不派生不存在的事实。
3. RED：`/workbench` 路由、刷新、Esc 锚点恢复、resume 重新请求。
4. GREEN：接入 state/index/render/footer，明确 Workbench 页面键位。
5. RED/GREEN：0/1/100 counts 与超长中文字段裁剪、语义颜色、无 ANSI 可读性。
6. 真实场景：SQLite Store→Service→Bridge JSONL→Node reducer→Overview renderer。
7. 定向 Ruff、compile、Node syntax/test；更新 UI-10 文档，自审后独立提交并推送 main。
