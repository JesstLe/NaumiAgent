# PA-04：跟进读路径与前台订阅

日期：2026-11-05～2026-11-06
基线：`777b35c80d279166cc92bb03aa0126588263db9a`
状态：实现、集中回归、同原对象 HTTP 前后对照、真实前台读取和撤权闭环通过；完整 24 项目标保持进行中。

## 实测问题与本次目标

上一模块的原路径冷重启实测：Today 建议读取 24.152 秒、原人工请求 29.044 秒，原 Day GET
9.192 秒；日历建议 16.493 秒、原人工请求 16.926 秒，原 Action GET 0.063 秒。
这不是 20 秒客户端预算足够的证据；上一轮增加独立 180 秒期限与停止等待，只解决可恢复交互。

本次减少重复执行，不扩大等待窗口，不引入跨请求权限缓存，不跳过末次授权。使用新独立 fixture
建立两条真实已采纳原目标，再以同一资料库、同一 native slot、同一来源和目标做 before / after
比较。旧 fixture 已撤权并清理，保持原证据，不复活或重播旧目标。

## 读路径修改

### 既有 effect 资料纯读取

原 `_read_target` 会调用带 ensure / publish / effect 写回职责的 `_effect_artifact`。现在独立为
`_read_existing_effect_artifact(current, initial, proposal)`：

- 只读原 `artifacts.read`，保留 get / `_checked` / 实际 blob / `_checked` 全链。
- 核对实际 canonical bytes、SHA256、冻结 effect revision 1 引用和完整 ArtifactRef。
- 已受理 effect 的 ref 缺失时返回不可投影，不由 GET 静默生成或修复资料。
- 原 apply / resolve / UNKNOWN 恢复继续由 ensure 分支负责，原 request/fingerprint/fence 不变。
- 原 `_read_target` 首次和末次来源证明都保留，原 Today / CalendarAction 读回仍是成功依据。

### 完整相同 day 快照去重

historical day 和 current day 的 scope、receipt.day_ref、placement、ItemRef、disposition 及 effect
artifact containment 仍逐份校验。只有 `(workspace, id, revision, full record_digest)` 全部相同，
才在本次证明阶段对 provenance 授权一次。不同 revision 或同 revision 不同完整内容仍分别授权；
用户实际移除或移动 placement 后，旧目标继续不可投影。不把该去重升级为 ACL 成功缓存。

### metadata 与 Item 共同证明

Today 新增私有 `_authorized_metadata_source`：先核对 expected ItemRef、metadata ref 和原历史
record，再按原 provenance 与 Item 授权逻辑返回 `metadata / original Item / current Item`。
旧 `_authorized_metadata_ref` 继续返回原 metadata，保持历史语义。deadline 来源复用这个证明，
不紧接着第二次遍历同一 Item。每个调用、最终来源阶段、下一次模型发送仍独立重验。

## 前台读取生命周期

Today 增加显式 `readActive`，与真实 `connected` 和人工写入状态分离。

- 跟进可见时，隐藏安排的 context / day / more / history 读取不启动，已在途读请求被取消，旧正文清除。
- 返回安排时重新读取原身份和原日计划，不将跟进期间的旧快照直接显示为当前。
- pending 存储、原请求恢复、人工写锁和已在途写入不因隐藏而清空或重置。
- 通知初始跳转、新 proposal prop、URL、namespace、断线和重连都有独立读取 fence。
- 原 Today **没有独立 15 秒 timer**。本次停止的是挂载、切换和 refresh 触发的隐藏读取；15 秒属于
  跟进自己的轮询，未将它误报为已经删除的 Today 定时器。

## 验收约定与状态

事务目录：`.naumi/transactions/pa04-followup-readpath-20261105/`。

1. 3174 tracked 原件与完整 baseline 源码副本已封存、逐 SHA 重开。
2. baseline 仅从冻结源码 import，记录产品 module origins、源码摘要与 34 个真实前端产品输入。
3. 原接口准备两次 accept、一个 Today placement 与一个 CalendarAction INSERT draft；无直接写 proposal / effect。
4. 计时期间关闭浏览器、构建、pytest 和后台 scanners。每阶段固定 10 次串行真实 HTTP，无自动重试：
   两种跟进 get 各 3 次，原人工请求各 1 次，原 Day / Action GET 各 1 次。
5. 每次记录 HTTP/body/proof、真实耗时与 ContextVar 限定 method counts，原方法 wrapper 在 finally 恢复；
   全数据库 schema/user_version/all-row digest 与原目标、回执、lease、provenance 保持不变。
6. 原来源撤权、实际 blob 损坏/缺失、跨 await 撤权、原状态变更和纯读零写仍需集中回归；不以更快的错误结果当加速。
7. 前端集中串行回归、生产构建及真实前台切换验收完成后再登记结果。
8. 最后只在完整源码副本演练 hash-guarded 回滚，保留主工作区优化，独立提交并重开四个交付物。

## 同原对象实测结果

基线和修改后分别固定 10 次真实 HTTP，计时无浏览器、构建、pytest、执行 worker 或 scanner。
两阶段使用同一个已构造资料库、同一 native slot 和原两个 accepted targets；正常响应的实际 wire
bytes 与 canonical SHA 逐项完全相同，每次 schema/user_version/全部行摘要、原 child fingerprint、
lease/fence 和 provenance 不变。首次样本不是操作系统磁盘冷缓存声明。

| 跟进 get，中位数，各 3 次 | 基线 | 修改后 | 本地减少 |
|---|---:|---:|---:|
| Today 已采纳建议 | 6.804619 秒 | 4.526484 秒 | 33.48% |
| Calendar 已采纳建议 | 3.735433 秒 | 3.264627 秒 | 12.60% |

这是同一原对象的串行观测，不与上一轮不同 fixture 的 24～29 秒混为加速对照，不代表生产 SLA 或容量。
原 Action GET 单次 0.0457 → 0.0501 秒有波动，未称提升。

方法计数提供独立依据：Today 单次 `_authorize_item` 20 → 9，`_proposal_source` 9 → 7，
OwnerJournal `_validate` 531 → 411；原 `artifacts.read` 仍为 1，实际 effect blob 的前后 `_checked`
保持，整体 `_checked` 24 → 12 是去掉重复授权展开，不是删除 blob 的末次检查。Calendar `_validate`
549 → 477，整体 `_checked` 3 → 3。

计数口径：checked_path 只覆盖 OwnerJournal 模块 global；原 `_target_request` 被既有 fixture
实例 audit shim 委托，类级计数在两阶段都未观测，不能把零解释为没有执行原指纹核验。
ContextVar 包装只计当前实际请求并在 finally 恢复原方法；inclusive time 不相加当 CPU。

## 集中回归、真实 UI 与自审

- 新只读 effect / blob / 跨 await 撤权组：13 passed；原 effects：21 passed。测试前后 5 文件 SHA 完全相同。
- Root 首组 1 failed / 64 passed，发现旧 `_view` 的末次授权异常会清正文但仍保留已设的
  `availability=available`。只修错误分支的状态，最终同一 65 项组通过；健康响应对照保持完全相同。
- 前端第一次串行 548 passed / 1 新测试异步 selector 失败，修正测试等待后 36 files / 549 passed；
  TypeScript / production build 和两个实际生产组件 bundle 均通过，生成 CSS 与原字节一致。
- 真实只读浏览器：初始直达跟进跨 27.303 秒、再次隐藏跨 15.509 秒，原 Today context/bulk day
  请求都是 0；显式原 Day/Action 读回保留；返回安排只各读取一次；namespace、真实断线/重连不
  启动隐藏读取。实际 mutation requests 为 []，page errors 为 []。
- 实际原 calendar 撤权前，exact-byte base、真实派生资料和 next-send 均允许；原 disconnect 后
  全部以 connection_revoked 阻断。自建服务 controlled exit 0，自有两端口释放；原 native key
  已缺失并由独立读回确认，不留下验收服务。

原全仓测试首项 1 秒启动超时与退出线程挂起保留为基线问题，仍不称全仓绿色；新实测不替代 Google
账户 live、发布安装、容量或多设备验收。源码副本回滚与最终独立提交由本事务 VERIFICATION.txt 收口。
