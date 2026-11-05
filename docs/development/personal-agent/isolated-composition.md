# 个人执行子 Engine 的统一构造与隔离

日期：2026-11-06

## 问题与目标

基线：`a983b71575ccabbf95e8d7822a658d33f731e27c`。
事务：`.naumi/transactions/child-composition-20261106/`。
修改前冻结并重开 3182 个原文件与独立回滚副本。

`personal/engine_execution.py` 原来直接构造 `AgentEngine`，虽然该构造仍会
按需建立依赖，但绕开了唯一 composition 入口。原全源码架构检查实际失败，
冻结副本也复现相同违规。本次不修改该检查，不通过新增例外消除失败。

## 实现

`runtime/composition.py` 现在同时提供普通与隔离工厂，两者调用同一个私有
`_compose_engine()`，由它建立完整 ports/resources/services 并执行产品源码中
唯一的 `AgentEngine(...)` 构造。

新增 `create_isolated_agent_engine(config, execution_key, nested_execution_key)`：

- 两个 key 均只接受小写 32 位十六进制字符串；只有 nested 的 `None` 表示无嵌套。
- 非法 key 在路径、端口、资源和服务 builder 之前拒绝。
- 工厂不接受任意路径、任意 child flag 或外部 execution directory。
- 只在隔离分支传入 `isolated_child=True` 和确切执行目录；普通入口的参数合同不变。

个人领域 helper 继续负责深复制配置、原 Job 的 permission/turn/cost/team token
预算、Job ID 的原 SHA256 key 和 peer message key，以及活的父级记忆策略绑定。
它不再构造 Engine 或自行建立运行时依赖。

## 必须保持的原状态

原目录与所有字段映射逐项保持：

```text
<runtime_data_dir>/personal-executions/<job_sha256[:32]>
<runtime_data_dir>/personal-executions/<job_sha256[:32]>/p/<message_32hex>
<execution_directory>/<sha256(field_name)[:12]>/<original_basename>
```

共享的 RuntimePaths 字段仍只有 `workspace_root`、`session_db_path`、
`runtime_data_dir`、`chat_run_db_path`；其余字段包括 harness/trust/evolution
均映射到原独立目录。RuntimePaths 原规范化和 containment 校验继续执行。

这里的“四个共享字段”不意味着所有领域库独立：TaskStore 与 WorkbenchStore
原本依赖共享 session DB，个人目录也继续使用原 owner/catalog。此次不迁移数据、
不改变这些作用域，不生成替代 Job/Run 或伪造新的持久身份。

child flag 在工具注册之前已经生效，避免替换父级 analysis Router、manager
和 pursuit 全局绑定。background/scheduler 继续使用原 execution directory；
父级记忆撤权仍能在 child 发送时生效。

## 验证与自审

新增测试逐字段对照原布局，覆盖 root/peer、同 key 稳定重开、不同 key 隔离、
非法 key 的零 builder 调用、完整依赖注入和父级全局绑定保持。相邻回归包括
事项执行、协作、团队、消息、记忆以及原全源码架构约束。

实际 HTTP/调度/子执行/工具/SQLite/原 Job 与 Run 的读回，以及冷重开与回滚
证据分别记录在事务中；受控本机模型协议与外部真实模型证据不混用。
最终命令、输入、字面输出、退出码和源码副本回滚见 `VERIFICATION.txt`。

### 本轮实际证据

- 冻结基线原架构检查失败；新增隔离合同测试在原源码上 **33 failed**，
  因原版本没有统一隔离工厂且 helper 仍直接构造 Engine。
- 最终集中合同、普通工厂及原产品全源码唯一构造检查：**56 passed in 10.49s**。
  新测试逐项检查同 key 关闭后重开原记录，root 和 peer 两种布局均保持一致。
- 扩大回归：**191 passed / 1 failed in 234.72s**。唯一失败是旧测试构造预算
  `234 <= 171`，冻结原源码也相同；本模块没有新增直接 Engine 测试构造，
  不调整该预算、不将扩大回归标记全绿。
- 真实本机 Uvicorn/API/SSE/调度/工具链：独立 Item 只提交一次 run-now，
  两个本机模型协议请求，实际读取捕获资料并计算 825；新进程原 Job、Run、
  report、delivery 精确读回，模型请求 0，完整逻辑数据库不变。
- 新独立真实模型事项：Item `pi_1f1f3462272d46bdb724f6e9f58179d4`，
  Job `agent-job-02fe64cb941c56ac2b9ae4cc6a1d6c9e419857c372b50c9a91b67a8942775a4f`，
  Run `bc5cbb62eb7d`。配置模型两次 Router 传输完成，实际 `file_read`
  原步骤校验 `/inputs` 路径、call ID 与 SHA256，回答 1200 − 375 = 825
  和 `CHILD_SOURCE_42`，自动原报告与 delivery 均读回；没有创建其他 Agent。
- 真实模型原记录新进程冷读：模型调用哨兵为 0，所有原 GET JSON 相等，
  原业务库逻辑状态不变；原报告内容和来源 Run 重新认证。
- 两类 harness 均实际关闭 Engine/API/模型服务并验证 owned 端口释放。
  真实模型没有假响应或新增重试；Router 调用数不冒充 SDK 内部 HTTP 尝试数，
  流接口未暴露原 provider response ID，成本也不冒充账单。

首次 lint 发现新测试函数签名 101 字符，已纯格式拆行；失败回执保留，
最终全 `src/` 与新测试 Ruff 通过。上述本机和真实模型证据分别存放，
真实模型验证使用全新的独立事项，不重放旧请求。

本模块不改 UI 或建立第二条用户命令路径；原 Tool/API/CLI/TUI 服务入口不变。
完整 Personal Agent 的 24 项能力、真实 Google 账户、发布与多设备验收仍保留
各自状态；修复一个构造接缝不构成完整产品完成。
