# Harness 不可破坏原则

以下原则是 H1/H2 的工程红线。后续阶段只能增强它们，不能静默放宽。

1. **精确信任**：只信任工作区规范路径与 Profile 原始 bytes 的 SHA-256 组合；一字节变化即失信。
2. **用户拥有信任权**：Agent 只能读取状态和知识，不能自行 trust/untrust。
3. **临时上下文**：自动知识只进入当前 `_messages` 的 Harness snapshot；不得写入 `_full_history`、长期记忆或 Profile。
4. **确定性优先**：没有 Eval 证明前不引入 embedding reranker 或 LLM 选择器。
5. **路径先于内容**：先证明 canonical containment、类型、大小、权限和敏感规则，再读取 bytes。
6. **预算是最终输出约束**：标题、路径、digest、围栏、正文和截断标记都计入预算；不得只计算原始文件正文。
7. **安全失败**：Profile/Trust/Git/文件读取失败时不泄漏仓库正文，不阻断主任务，并给出中文下一步。
8. **单一 Service**：用户命令、Agent Tool 和 Engine 注入必须共用 `HarnessService`，不在 UI 复制知识逻辑。
9. **不执行命令**：H2 不运行 `.naumi/harness.yaml` 中的 argv；命令执行必须等待 H3 的 allowlist、timeout、取消和证据闭环。
10. **不吞掉新鲜度**：每轮精确校验 Profile 信任和已选择 source digest；已知候选检查 metadata，Git 定期审计，写工具成功后立即失效；L2 读取再次校验 digest。
11. **跨平台 argv**：不得依赖 shell 管道、bash-only quoting 或 macOS-only 路径语义。
12. **真实验证**：每个阶段至少使用真实文件系统、Git、SQLite 和真实仓库路径完成定向测试，不用 import 成功冒充端到端可用。

如果实现需要违反其中任意一条，应先修改批准后的架构设计并给出安全证据，而不是在代码中加入隐藏例外。
