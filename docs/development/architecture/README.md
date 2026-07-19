# Future Architecture 后续模块册

本册细化 `docs/14-future-architecture-refactor-plan.md`。路线仍坚持：先在 Python 单体内部建立
边界，再服务化 Runtime；TypeScript/Ink 和 Rust/Go daemon 只有达到量化门槛才采用。

## 目标分层

- `naumi-core`：模型、契约、领域类型、纯策略。
- `naumi-runtime`：会话、Agent 循环、任务、Harness、权限协调。
- `naumi-tools`：工具实现和执行适配。
- `naumi-frontends`：New UI、TUI、Workbench、未来 Web。
- `naumi-daemons`：浏览器、shell、重执行、集群 worker。

模块顺序：ARC-01/03/05 → ARC-02 → ARC-04 → ARC-06 → ARC-07/08。

ARC-01.4c1-4c2 已交付由 Composition Root 构造的首个 `RuntimeServices` 切片、共享 terminal runtime lifecycle
factory 与 New UI adapter 迁移；TUI adapter、其余 Service 与全局关闭注册表仍未完成，因此 ARC-02 退出门尚未满足。
