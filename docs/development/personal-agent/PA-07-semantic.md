# PA-07：独立 Agent 研究复核

## 本轮范围

用户在已有研究中选择另一个自己创建的 Agent，预览确切来源与共享范围，确认后启动
独立的持久执行，逐条保存支持、反驳或依据不足。初始目录仍只有主 Agent，系统不预置角色。
复核不读取作者的整段日常对话，也不继承对话的全部资料或复核者的自动检索记忆。

复用既有 Collaboration、AgentJob、ChatRun、Session、Artifact 与 Memory，不另建第二个
执行器。机械引用/数值检查继续保留 `semantic_verification=not_performed`；独立结果另行
标记 `verification_scope=provided_sources`，不声称已经核实来源之外的所有外部事实。

## 实现与状态

### 1. 确切写作依据

- Notebook 的新建和每次修订保存不可变 `provenance`：入口、原作者版本、原 Session、
  确切 Artifact 引用、真正选入运行的原始 Memory 引用及 Item 范围。
- 原 Memory 的修订、删除和撤权参与实时校验，不只依靠其附属 Artifact。
- 保存意图先冻结 notebook/provenance，提交前再核对；中断后的同请求不重新选择依据。
- 人工修改继承历史依据，人工与模型入口不能通过原请求互换。旧记录继续可读，
  缺失的历史依据保持 `legacy_unknown`，不补造为已知空依据。

### 2. 复核资料合同

- 预览遍历 Notebook、上游修订、完整引用来源以及 Artifact 的历史 read/context/provenance
  祖先。每个材料与 Memory 都要具有原持有者和接收方的确切授权。
- 原作者自有材料可以在用户确认后向所选 Agent 分享；第三方所有的材料必须已经获得
  该所有者向所选 Agent 签发的授权，当前作者不能代所有者重新分享。
- `semantic_input` 冻结资料及双方授权；`peer_read` 绑定输出字节和这份不可变输入。
  临时历史解释范围只从库中逐字节一致的不可变输入建立，不构成全局 ACL 豁免。
- `authority_version=9` 使旧 Artifact broker 拒绝新凭据，而不是忽略 Memory 或复核授权。
  派生版本保留原证据祖先，修改正文或省略 evidence 不能清除原约束。
- Research 协作计划的 SQLite `user_version=2` 使旧执行器拒绝解释新计划，防止回滚旧代码后
  把资料包委派当作普通全会话委派恢复。仅含普通旧计划的资料库仍使用版本 1；已有研究库的显式兼容提升
  仅改元数据并核对所有业务表摘要，GET 不迁移、不降级、不派发执行。

### 3. 独立真实执行

- 复用一个 `review` DAG 步骤，绑定所选 Agent 的确切版本和唯一资料包。
- 执行使用独立 Session/Job/Run，只开放 `personal_research` 复核操作；无网络、shell、
  自动记忆检索、额外互询或作者完整会话传播。
- `semantic_sources/source` 从冻结的确切来源版本分页返回正文，记录字符和 UTF-8 字节位置。
- 目录与正文按实际 JSON 长度控制分页，保持在现有工具结果归档阈值以下；大目录返回有确切
  摘要和续读位置的完整 JSON 分片，不以不可访问的归档占位代替输入。原文候选锚点由代码计算，
  只作为位置候选，不预设支持/反驳结论。
- `semantic_submit` 要求逐条主张结果，支持/反驳必须提交真实来源摘要、原文及字符起点。
  服务端校验正文、版本、哈希、范围和逐字匹配，生成字节锚点；这不代替语义判断本身。
- 引文还必须被同一原 Run 的前序模型轮次实际返回的已完成正文页连续覆盖；原提交完整保存
  Job、Session、Run、模型轮次和页面范围。跳过正文、跨 Run/来源、同轮工具批次抢先提交均被拒绝。
- 每次入场、续租、工具和模型发送边界重核当前依据；单次内部授权只缓存确切不可变记录，
  不缓存最新研究/来源 head 或 Artifact/Memory grant。嵌套工作结束即销毁缓存。
- 完成必须同时满足原 Job、同 Session 的原 ChatRun 和结构化提交；自然语言声称完成不算。
  UNKNOWN 只对账原身份，不自动新建 Run。取消与资料撤回继续走原执行边界。
- 已保存原协作但丢失 start 回执时，原请求 GET 通过不可变绑定投影已受理状态，不重发 Job，
  不回写 intent，也不修复 Session 执行标记。尚未受理的准备请求仍要求显式重试原 start。

### 4. 入口与交互

所有入口共享同一服务与严格 payload，不从客户端接受 caller、权限凭据或执行结果。

| 入口 | 合同 |
|---|---|
| Tool | `personal_research` 的 `semantic_*` 操作；复核提交只在绑定执行中生效 |
| Web | Research 中选择已有 Agent、预览资料、确认复核、逐条结论、原执行对话 |
| HTTP | 4 个只读 GET 与共享 `/personal/research/operations/{operation}` |
| CLI/TUI | `/research semantic-preview-json`、`semantic-start-json`、`semantic-get`、`semantic-list`、`semantic-request`、`semantic-reconcile-json`、`semantic-cancel-json` |

前端保持浅色布局，不加入宣传文案。历史版本、未保存草稿、只读执行和作者 Session 不匹配时
不允许新建复核。断线只保存 opaque identity，恢复先查原请求；切换工作区、Agent 版本或权限
后，晚到响应不修改当前界面，后续状态也不得替换首次非空的原 Job/Session/Run 身份。

## 验证记录

本轮事务：`.naumi/transactions/pa07-semantic-20261106/`。
原提交 `e94c3a011c75795bba6df6a1d5f858de75a0c183` 已完整冻结，原始哈希保存在
`baseline-manifest.json`。最终专项、真实模型、HTTP、冷重开、源码副本回滚及提交结果以该目录
`VERIFICATION.txt` 的实际命令、原输入、literal output 和退出码为准。

开发中的验证按层记录，不将接口替身测试视为真实模型或外部账户验收。

### 本轮实测收口

- 分批专项与相关回归共观察到 **1200 个去重通过 nodeid、2 项 Windows 链接环境跳过**。
  其中原大回归在 1200 秒边界已通过 652 项、没有断言失败；仅续跑未完成项，62 项通过。
  这些是不同修订上的分批证据，不称末版一次性全项目 1200 项通过。完整运行与去重清单见
  `module-observed-coverage.json`，修订和失败历史按各批收据保留。
- 完整前端 **40 files / 674 tests** 通过，TypeScript 与生产构建通过。
- 独立新请求 `real-fixture-04` 从真实 UI 确认发起：1 个原 Job、1 个独立 Session、1 个
  ChatRun，**7 次实际模型调用**。模型分页读取完整 28360 字符 RFC 文本，保留一次提交格式
  错误及同 Run 的纠错，最终结构化结果 `literal-lowercase=supported`、
  `uppercase-true-valid=contradicted`，原文、摘要和字符/字节位置全部读回核对。
- 原请求重复 GET 没有新 Job/Run；独立进程冷读 3 个真实 API GET 及原结果，
  0 模型、0 网络、0 执行器恢复，全部 SQLite 业务库存到 shutdown 后仍相同。
- 新进程生产 UI 冷重开另外完成 38 个 GET、0 应用写入，原复核/Run/结果摘要相同；
  桌面 1440 与手机 390 的结果和原只读执行 Session 均已实际截图并目检，
  无横向溢出或 console/page error。所有验收 host 已停止，没有留下后台模型执行。
- 实际冻结旧 Artifact reader 拒绝 authority 9；新 reader 能打开显式提升为 schema 2 的
  原计划，冻结旧 Collaboration reader 返回 `unsupported_schema`，未重写任何原业务行。
- 全量 `pytest tests/ -x` 保留原 live/environment 结果和协议 fixture 摘要失败；同一实际
  工作区输入由冻结源码与新源码产生完全一致的 `fixture_digest_mismatch` 报告，6 个 fixture
  文件与原始哈希相同。本轮不改写这些既有摘要，也不以模块通过冒充全量门槛通过。

原请求 `3f9a802eb2434728b3e034788ea0beb4`，复核
`rsv_58c9b396e79c715f7c8ec7caeba9d4e2`，ChatRun `86f829c23f76`；结果摘要
`6685da53e5c9948934a5cc4b2e65735b1695bd31a5ace521b69f43d993013170`。

首个真实模型 Run 在第六次调用提交纠错时，被验收脚本的旧 240 秒超时/强制停止逻辑取消；
原 Job 与 Run 均保留 `cancelled`，没有结构化 submission 或结果 Artifact。失败收据未覆盖、
原付费调用未重放。验收脚本改为终态前不停止，并使用独立 fixture/请求继续验证；真实结论
仍以最后的实际收据为准，不把已读正文或模型声称提交当作完成。

## 自审与未完成项

- 此模块核验提供的来源，不提供跨主题研究质量、全面事实核查或预测准确率的保证。
- 后台 Item 研究 policy、审批化外发的 Item 执行和可靠送达仍需独立接入与验收。
- 真实 Google 账户 OAuth/事件写回读回、手机/其他设备、语音与电脑控制、部署和持续运行
  均沿原路线继续，不由这一模块的本地证据推定通过。
- PA-07 与 24 项完整产品目标保持分级状态；单模块完成不将总目标标记 complete。
- 本轮真实 RFC 原文件含 16 处 form-feed，现有研究导入的文本控制符合同将其拒绝。真实验收
  另导入明确记录 `0x0c → 0x0a` 的派生文本，保留原文件、两个摘要和 Artifact 父来源，
  不称字节等价，也不将原失败改写为成功。原始分页控制符的通用导入支持仍待独立处理。
