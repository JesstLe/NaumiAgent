# 其他模型交接提示词模板

## 1. 模块领取提示词

```text
你将在 /Users/lv/Workspace/NaumiAgent 实现模块 <MODULE_ID>。

先完整阅读：
1. docs/development/README.md
2. docs/development/<模块文档路径>
3. docs/development/02-delivery-review-protocol.md
4. docs/development/03-acceptance-evidence-standard.md
5. 该模块直接依赖的源码与测试

约束：
- 不使用子 Agent，除非用户重新授权。
- 一个模块一个提交；不要实现相邻模块。
- 先写测试并观察正确 RED，再写生产代码。
- 只运行本模块定向测试，不运行全量测试。
- 保护用户未提交改动；不得 reset/checkout 覆盖。
- 用户可见文案中文，commit message 英文。
- 真实场景不能 mock 目标边界。

开始前报告模块 ID、依赖、目标文件、非目标和验证命令；完成后按交付协议提交证据包。
```

## 2. 实现过程追加提示词

```text
继续 <MODULE_ID>，不要扩展范围。若发现文档与源码不一致：
1. 先给出文件/行/测试证据；
2. 判断是文档漂移、实现缺陷还是依赖未完成；
3. 不擅自重写架构；把必要调整限制在本模块并更新文档；
4. 若会改变公共接口或模块依赖，停止并请求审核者决定。
```

## 3. 交付提示词

```text
为 <MODULE_ID> 生成最终交付证据包：commit、文件、RED、GREEN、lint/compile、真实 smoke、
错误/并发/恢复/安全/跨平台结果、未运行项、已知不足、后续模块。确认 git diff 只含本模块，
不要宣称未实际运行的验证通过。
```

## 4. 最终审核提示词

```text
你是 NaumiAgent 模块 <MODULE_ID> 的最终审核者。只审核，不先修复。

阅读模块文档、交付证据包和 docs/development/06-final-audit-checklist.md；检查真实 diff、接口、
Store/协议、安全、UX 和定向测试。重新运行风险最高的最小测试与真实场景。逐条输出：
- 规格符合项
- 按 P0/P1/P2/P3 排序的问题，附精确文件和行
- 缺失或不可信的证据
- approved / changes_required / blocked / rejected

若 changes_required，修复建议必须保持一个模块范围，不得借审核重构相邻系统。
```

## 5. Codex 主审核约定

用户后续把其他模型提交交回时，Codex 应先读取模块 ID 和证据包，核对当前 main 是否漂移，
再按审核提示词执行。审核通过后才更新 `module-registry.yaml` 与人工注册表状态。
