# PA-02：个人记忆与实际模型上下文

## 模块状态

本模块在 `codex/personal-agents-pa00` 上接续 PA-01。基线为
`4f7d86a451cd38b5fbc84cf8e090fac7768ee63d`。
Store、自动提取、Service、接口、前端已分别完成组件验证；Root 的统一回归、
真实模型、真实浏览器、冷重启和副本回滚以事务目录的最终记录为准。
尚未形成证据的环节不计为通过，不代表 PA-00～PA-11 总目标完成。

## 用户入口

- Web2 的“记忆”导航进入实际管理界面，不是浏览器 localStorage 记事本。
- `/memory` 在 CLI、TUI 和 Web 手动命令中使用同一 Service。
- `personal_memories`、`memory_store`、`memory_recall` 由当前 Agent 自主调用；模型只能
  查询或提出候选，不能通过传参获得人工确认、纠正、删除或分享权限。
- 初始只有主 Agent。其他对象是用户要求创建的独立 Agent，不预置职业类型或名称后缀。

## 唯一事实与范围

`PersonalMemoryStore` 使用 owner-bound SQLite。Owner 来自实际个人目录的规范路径，
不是 workspace；两个 workspace 可以复用同一 owner 的个人偏好，仍分别保留项目与事项范围。

| 维度 | 规则 |
|---|---|
| actor | 主 Agent、真实 Agent 或团队自身；团队不继承成员记忆 |
| personal | 跨工作区，但不自动跨 Agent |
| workspace | 实际工作区身份 |
| item | 实际工作区与确切事项身份 |
| 分类 | preference / fact / decision / experience / plan_template |
| 状态 | proposed / active / superseded / rejected / deleted |
| 覆盖 | 同主题 item > workspace > personal；同范围 owned > explicit shared |

空槽的人工输入可以直接 active；已有 active 时保留旧值并形成冲突候选。
替换须人工提交候选版本、正文 SHA 和预期 active ref，SQLite 事务仲裁并发赢家。
不使用“相似就合并、保留较长正文”的向量去重方式改写用户事实。

## 来源不等于确认

`source` 保留原作者、位置、摘要、可擦除摘录与资料确切 refs；`confirmation` 单独保存人工回执。
助手建议经人工采纳后仍显示助手来源。自动提取分别扫描 user / assistant，排除转述、代码、
被否定的事实与假设，保留明确否定偏好的原含义，结果始终是 proposed。普通消息与压缩结果保持 workspace / Item ceiling；
跨项目个人摘要必须由用户单独输入，不能靠关键词“个人偏好”扩大原资料范围。

消息的 `source_mode=captured_snapshot` 表示已捕获快照；`locator_live` 另外说明原消息是否仍可定位。
聊天保留期到期不会自动遗忘已确认快照，也不将快照冒称实时原消息。
Artifact 来源及上游记忆依赖仍逐次校验当前版本、SHA、原授权与回复路径。

模型生成的候选还记录实际消费的上游 memory refs。撤销原 share 后，后来发出的新 grant
不能顶替旧依赖；普通模型摘要不能洗掉来源约束。定位和不可变审计不复制正文。

## 删除、纠正、草稿与恢复

- 正文和摘录在可擦除 payload，历史与请求回执只保留身份、范围、摘要和操作事实。
- revise / supersede / delete 同事务擦除旧 payload / FTS，撤销旧 grants。
- 同原 request 重放只投影当前 head / tombstone，不复活旧正文。
- GUI 本地只保存 opaque 请求身份。原 payload 在同一 SQLite prepared 表内，apply 后擦除。
- discard 与 apply 在真实 SQLite 事务中竞态：discard 先赢则迟到 apply 拒绝；apply 先赢则返回
  applied 当前事实，清理草稿不被描述为取消已经完成的记忆变更。
- 丢失响应、503、缺行或原请求 404 不等于未受理；保留原 ID 继续核验。
- 目录、DB 或 instance 丢失由独立 witness 检出，不静默新建空库。

删除记忆不代表删除原聊天、Library 文件、用户导出的 JSON 或已发往 provider 的上下文。

## 检索与多进程修复

SQLite FTS5 使用实际英文/CJK token。语义通道使用本机已缓存的 ONNX MiniLM，不自动下载，
也不使用 hashing embedding 冒充真实语义。候选在 top-k 前完成 actor / scope / 来源授权。
RRF score 与 semantic similarity 分别呈现，不将排序分数称为概率。

实际双进程 probe 证明共享 `PersistentClient` 的 HNSW cache 不一致会产生 `Error finding id`。
本实现改为 **Chroma 进程内派生 cache**：

1. SQLite head、来源和 index outbox 持久；Chroma 不保存 documents 正文。
2. 每个进程按实际 owner、规范目录和模型 policy 标识自己的 collection。
3. 每次 query 对已授权 exact revisions 执行缺口 repair。
4. 完整 sync 用所有当前 active heads reconcile，清除本进程旧向量；query 子集不能剪除其他 actor。
5. 同进程同 identity 共用锁，embedding 调用共用锁，避免 repair/delete/query 互相穿插。

冷启动或 cache 丢失只重建派生检索，不改变记忆确认。旧 `naumi_memory` 仅通过人工 legacy
入口读取与显式导入为候选；Engine 没有旧全局 Chroma 召回旁路。
旧库读取使用 SQLite `mode=ro` / `query_only`，不初始化 Chroma SDK、创建集合或迁移原资料库。
实际 Chroma fixture 的读取前后逻辑 dump 相同；缺库不 bootstrap，不兼容数据保留原件并返回明确状态。

## 实际模型发送边界

Engine 的普通 call、超长恢复 call 与 streaming send 使用同一个准备接口：

```text
sanitize（保留 provenance metadata）
→ runtime 消费 refs，复核当前记忆与来源，构造新的发送列表
→ 私有来源 Artifact refs 固定到实际 Session
→ 记录 ref-only model_send_prepared 证据
→ 最后移除 provider 不接受的 metadata
→ 实际 ModelPort
```

记忆正文不写入持久 system block，工具结果只持久化 refs。旧全局注入与旧工具正文被隔离；
关闭记忆也清理 raw / metadata-only 旧引用。准备证据不是 provider 接收或完成回执。
本轮引用被纠正、删除或撤权后，后续模型或工具边界停止旧上下文执行。

ContextCompactor 的提取与摘要每次内部模型调用也经过执行授权，不仅主 ReAct call 受检。
compaction 兼容适配器只写 scoped assistant 候选，不写 confirmed global memory。

## 委派与私有来源

PA-01、协作步骤和 peer child 保持旧 long-term memory 配置关闭，由可信运行时 context 指定：
执行 actor、实际 origin、全部祖先回复路径、workspace、Item 和冻结的输入 Artifact refs。
每一位实际回复接收方均须有同一当前记忆的读取权限。未绑定 child 不读取记忆。

事项的 origin 来自经 definition SHA 校验的原始委托，不把 assignee 错当 origin。
模型任务中的 user role 不是新的人类原话。子执行不能借来源记忆扩展其冻结输入。

读取私有来源记忆会持久固定 Artifact session refs，避免下一轮检索 miss 后，历史答案失去
私有来源标记。宿主进程、网络、公开文件写入及浏览器使用既有私有资料边界；memory DB、
witness 和旧 vector 路径也受保护。

## 证据位置

`E:\Workspace\NaumiAgent\.naumi\transactions\pa02-20261104\`

保留组件失败、修复后的结果、实际跨进程 probe 和审查记录。最终四产物、真实场景、
冷重启与 rollback 状态由 Root 在完成后追加，不用旧测试结果覆盖新分支的待验状态。

## 最终本机验收（2026-11-05）

| 验证层级 | 实际证据 |
|---|---|
| 原记忆模块基线 | 80 passed / exit 0 |
| 全相关后端 | 1902 passed / 1 failed / 2 skipped；唯一失败为未修改的 PA-01 ItemStore WAL 首次并发建库，HEAD 隔离源码同测试复现 |
| 最后 schema / 只读 legacy / Engine 集成 | 434 passed / exit 0；不与上一组叠加计数 |
| 完整前端 | 30 files / 311 tests passed；生产构建通过 |
| 实际浏览器 | 创建、纠正、修订、JSON 导出、删除、丢响应后的原 ID 草稿清理；本地不保存正文；1440 / 390 均重新打开检查 |
| 实际 provider | A main、B main、Echo 未分享/分享/撤销，共 5 个成功 Run；独立修复验证模型自主调用 propose，共 6 个成功 Run / 7 个完整 provider 调用 |
| 保留失败 | 原第六 Run 因 enum schema 被 HTTP 400 拒绝，terminal failed，不重放；原费用保持 UNKNOWN；修复使用新的独立 Session/任务/请求 |
| 删除 | 真实助手候选初态 proposed / assistant / human=null；删除后 payload / FTS 为 0；旧 active ref 失效；原请求重放仍为 tombstone |
| 冷重启 | 实际服务器重新构造 Engine，原 owner、记忆、确切授权、来源、Session/Run/回执读回；实际 ONNX 检索；新增 paid Run 为 0 |

冷重启前后原始持久 SHA **不相同**：真实 `load_session → resume` 只更新原 UI 会话的
`last_accessed_at`。Root 保存重启前整表行并与第一快照 SHA 核对，逐列证明仅该字段变化；
其余 13 库逻辑状态、业务快照和所有原 Run 均一致。访问时间不被当成业务变更，也不把
原失败 Run 改称成功。记录见 `cold-restart-reconciliation.json`。

源码四产物、完整命令/输出/退出状态、51 路径副本回滚和后改零写守卫以同目录
`VERIFICATION.txt` 为最终证据。源码回滚不自动降级用户记忆数据库。
本机验收不代表完整产品、发布安装或多设备平台验收；24 项要求仍全部保留。

### 最后发送窗口自审

实际发送准备包含异步的引用解析与 ref-only 证据写入。新增受控竞态测试在解析后、transport 前删除原记忆，在初始提交 `2e07a7e0` 上真实复现 `1 failed`：旧上下文仍进入受控模型端口。现在三个发送入口共用的准备接口在解析与证据写入完成后，再执行一次 `authorize_execution`，最后才移除 metadata 并进入实际 ModelPort。修复后的完整影响组为 **434 passed in 85.33s / exit 0**，包括来源、生命周期、compaction、委派、兼容入口与该竞态；测试中的 transport 调用数为 0。未重放任何已付费 Run。

初始本机模块提交与此窄修复分别保留，不改写历史；最终四产物对当前文件重新冻结。早先 `snapshot-v1`、`snapshot-v2` 源码包保留供比对。
