# ARC-01.3f 可撤销 ToolRegistry 原语

## 目标

为 EVO-06.3b2c 临时 Registry lease 和 CC-04 扩展运行时隔离提供共用的最小注册表边界：动态代码只能
在无 exact name、无旧 namespace alias 冲突时注册；撤销只能删除 lease 自己安装的同一 Tool 实例。

本切片不加载 Capability Artifact、不签发 lease、不改变模型可见工具，也不移除可信内置工具使用的旧
`register()` replace 行为。它只补齐后续 authority 能安全调用的原子内存原语。

## 问题边界

旧 `ToolRegistry.register()` 直接覆盖同名 key；`get()` 又兼容 `default.foo` 与 `default__foo` 到 `foo`。
因此仅检查 exact name 仍可能让动态扩展形成 alias 阴影。简单 `del registry[name]` 也存在 ABA 风险：过期
lease 可能删掉同名的新实例。

## 契约

- `register_unique(tool)`：在一个 `RLock` 临界区检查 exact name 和双向 legacy alias；任一冲突都抛出
  中文 `ToolRegistryConflictError`，注册表保持不变；
- `get_exact(name)`：不执行 namespace fallback，供 authority 比较真实安装实例；
- `conflicts_for(name)`：稳定排序返回 exact/alias 冲突，只读且不修改状态；
- `unregister_if_same(name, expected_tool)`：仅当 exact key 当前仍以 object identity 指向该实例时删除；
  被替换、已撤销或名称漂移都返回 false；
- `all()`、`names`、`get_openai_tools()` 与读写操作使用同一锁，调用者不会观察 check/set 中间状态；
- 可信启动与显式 hot reload 继续使用 `register()`，保持现有兼容语义；动态 Capability/Plugin 不得使用它。

## 验收证据

- [x] exact、`default.foo`、`default__foo` 三类冲突均拒绝且原实例不变；
- [x] compare-and-remove 不会删除同名的其他实例；
- [x] 八线程竞争同一名称时只有一个 `register_unique()` 成功；
- [x] 现有内置注册、legacy namespace lookup、OpenAI schema 和参数解析小模块测试保持通过；
- [x] Ruff、py_compile、文档治理和 diff check 通过；未运行全量测试。

## 下一步

EVO-06.3b2c 可以在此原语之上实现持久 lease authority，但仍必须独立完成：current passed 3b2b Receipt
重验、候选从 sealed source 的受控 materialization、lease ID/digest/expiry/epoch、Runtime 重启恢复策略、
到期/来源漂移撤销，以及 CLI/TUI/New UI 可见状态。本切片本身没有授予任何候选执行权。
