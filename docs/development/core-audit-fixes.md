# 底层审计修复记录（2026-11-04）

审计基线：`df444987`。原始证据：`E:/Workspace/NaumiAgent-audit-20261104-df444987/`。
本任务工作树：`E:/Workspace/NaumiAgent-fix-core-20261104`。保留原工作区并行开发改动。

## 会话持久化（F01 / F04 / F08）

- 完整数据库操作使用同一异步锁；初始化只发布完成迁移的连接；取消和异常在释放操作锁前完成 rollback。
- `save()` 消费并关闭 RETURNING cursor，再 commit。
- 新增 `update_metadata()`，在事务内读取最新记录，只更新指定字段。标题修改不写旧消息快照，system prompt 修改基于最新历史。
- 会话 HTTP 修改与执行锁一致，内存中的标题/模型/系统提示同步，防止下次保存覆盖修改。
- 复制会话保留 `engine`，消息列表仍为深拷贝。
- 新回归基线：6 failed, 2 passed；修复后会话相关 7 个模块共 51 passed。
- 已验证：真实临时 SQLite、并发保存/恢复/置顶、取消回滚、改标题竞争、两种引擎复制。尚未将该修复合入有并行改动的原工作区。

## 全部模块已完成

- F02：本地执行进程树生命周期，详见 `core-process-fix.md`。
- F06/F07：浏览器恢复/终止竞争与 runtime 归属，包含复审发现的清理期间重复取消边界，详见 `core-browser-control-fix.md`。
- F03/F05：增量脱敏、历史一致性与断流恢复，详见 `core-stream-fix.md`。
- F09：七个工具精确权限规则，详见 `core-permission-rules-fix.md`。
- F10：协作大权限载荷持久化与完整审批显示，详见 `core-collaboration-permission-fix.md`。

## 与并行开发的集成

- 合并已提交的 `8f8d4bf9` 专员消息功能，保留执行身份标记、peer scope、消息和独立会话接口。
- 会话更新等待异步执行记录检查，复制会话同时保留引擎和执行身份。
- 大参数审批适配 `_attach_interactions`：根会话校验不放松；peer请求必须匹配真实只读 PersonalMessageStore 记录的 workspace/collaboration/step/receiver_session/recipient_actor。
- 根/peer 两种审批均完整展示参数。70 KB ASCII 与 96 KB 中文写入在真实工具链中获批后执行；由接收专员再调用真实 pytest 核验完整文件 SHA-256，最终完成回执为 completed。拒绝时不写文件、不执行验证。
- 不复制、覆盖、stash 或恢复原工作区继续开发的未提交团队会话改动。

## 综合验证与测试维护

最终四批选择了39个测试模块，共 **1304 passed, 1 skipped**：
- 核心/会话/进程/浏览器/模型/权限/事件与API：573 passed, 1 skipped。
- 完整引擎模块：155 passed。
- 个人助理/协作/peer/权限载荷：549 passed。
- 独立Worker路由/模型执行/工具RPC/进程：27 passed。

唯一skip是Windows上既有POSIX执行夹具；全部新增回归均执行。不是全仓所有测试或所有平台的验收。

综合测试还定位并修正了已有测试夹具问题，未降低产品门禁：
- missing-session测试错误等待resume，但生产在候选load缺失时已退出。测试改为拦截实际preflight load，并对事件等待设5秒上限，保留权限grant/状态断言。
- 三个可视化单元测试控制blueprint.execute，须明确选择进程内执行路径；独立Worker契约由另四模块27项单独验证，避免测试误调用未配置的真实模型传输。
- Schema缓存测试检查内部模板缓存不重建及授权投影内容，而不要求动态授权投影列表保持同一对象。
- Windows无symlink权限时使用真实目录junction验证同一沙箱逃逸断言，没有跳过路径校验。
- 初次大批测试480秒超时、诊断180秒超时及中间失败记录保留；修正了证据runner的timeout stdout类型处理并恢复默认记忆配置，最终完整引擎155项通过。

详尽命令、退出码、原始输出、基线与回滚将封存在 `E:/Workspace/NaumiAgent-fix-core-evidence-20261104/VERIFICATION.txt`。
