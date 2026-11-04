# 底层权限注册修复（F09）

## 完成内容

补齐当前已注册但被普通权限模式判为 UNKNOWN_TOOL 的七个工具。全部为精确名称规则，没有新增名称前缀放行，也没有修改 bypass 行为。

- `agent_result_acknowledge`：LOW、无需额外确认。真实执行仍由当前 session、delivery ID、digest fence 约束，只记录幂等已读，不删除结果。
- `output_asset_retention_run`：HIGH、每次确认、禁止 session grant，保留已有的旧文件/持久引用/删除数量检查。
- 五个 capability specification/governance/artifact/scenario_binding/sandbox_request 工具：MEDIUM，每会话上限 50 次，允许 permissive/moderate/strict，不允许 lockdown。
- 这五个工具是规格、人类治理、静态制品、场景与请求封存，不 import/注册/执行候选工具。内部持久人类交互、当前规格/制品绑定及干净 revision 检查未改变。
- `source_path` 继续使用真实工具 metadata 的路径沙箱检查；下游 sandbox_execute 的 bash_run 委托闭包和执行回执保持原样。

## 验证

基线新回归：`46 failed, 4 passed in 7.02s`，退出 1；失败均来自缺失精确规则或其结果。

修复后通过真实 Tool metadata、PermissionChecker、注册 AgentEngine 以及已有输出资源清理/进化资料权限测试：

```text
89 passed in 6.60s
SUITE_EXIT=0
All checks passed!
RUFF_EXIT=0
```

回归覆盖：不同非 bypass 模式、lockdown 拒绝、五种工具限额、越界源码路径、近似未知名称拒绝、逐次清理确认、无 session grant、原执行委托闭包和整个引擎注册表都有权限规则。

使用主仓 `.venv`，显式加载修复工作树源码；测试 HOME、USERPROFILE、workspace、会话/向量/personal 目录隔离，未调用真实模型。精确命令与输出位于外部证据目录 `permission-baseline.txt`、`permission-modified.json/txt`。

最初一次修复后命令使用错误的 pytest 类选择器 `TestInit`，收集退出 4，未执行测试；已纠正为 `TestEngineInit` 并保留 `permission-collection-error.txt`，不计入通过结果。

## 自我审视

未用“注册即全权限”掩盖缺失规则，也未让删除变成无需确认。新增规则不改变内部人类授权与实际执行回执语义。注册/权限/真实本地清理链路通过，不代表对全部能力工具做了真实外部模型或生产端到端验收。
