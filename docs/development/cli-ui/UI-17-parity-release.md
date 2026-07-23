# UI-17 New UI/TUI Parity 与发布门

## 目标

定义默认新 UI 与 TUI fallback 的核心能力契约、版本兼容和发布阻断条件，避免某一表面静默落后。

## 核心 parity 契约

提交/流式响应、工具生命周期、权限确认与 bypass、取消、任务/Agent、Harness Receipt/Explain、
history/resume、doctor/debug、模型/provider identity、budget/context、退出和错误恢复必须两端可用。

允许差异：新 UI 可有全屏布局、鼠标和动画；TUI 可用线性/侧栏降级，但不能缺失操作。

## 子模块

- UI-17.1 Capability manifest：已实现；每个前端声明协议版本、14 项产品 capability 与真实证据，见
  [设计与验收](UI-17-1-capability-manifest.md)。
- UI-17.2 Golden scenarios：partial (17.2a-17.2f)；runtime-health 已使用同一 fixture 对 Bridge、TUI 与 Node reducer
  断言语义字段，见 [设计与验收](UI-17-2a-runtime-health-golden.md)；permission/bypass 与 model-initiated interaction 已
  锁定脱敏请求、canonical choice/answer 和终态，见 [设计与验收](UI-17-2b-permission-interaction-golden.md)；
  submit/tool/receipt/cancel 已锁定统一输入、类型化消息、完成终态和 Ctrl+C 行为，见
  [设计与验收](UI-17-2c-terminal-run-golden.md)；17.2d 将 EVO-03.7a 单 lane 回执接入 New UI typed 专页，
  并以共享 fixture 锁定 TUI fallback 的 RED/GREEN、资源和非最终语义，见
  [设计与验收](UI-17-2d-evaluation-lane-receipt-golden.md)；17.2e 锁定 token 合并、相关 error 的断流收尾、TUI 失败状态与
  New UI 本地发送 retry identity，见 [设计与验收](UI-17-2e-terminal-stream-recovery-golden.md)。进程断连后的 uncertain
  恢复、权限中断恢复与完整 Harness snapshot 仍待实现。17.2f 已用同一 terminal lifecycle fixture 生成 New UI 与
  Textual production compositor 的固定尺寸 ANSI/text frame、digest 与共享语义锚点，见
  [设计与验收](UI-17-2f-terminal-golden-capture.md)；它是 capture 原型而非跨平台发布门。
- UI-17.3 Compatibility negotiation：partial；17.3a 已让 Evaluation Lane typed event 在新旧 Bridge 组合中按
  协商能力进入专页或降级到共享 Slash 通道，并阻止未协商请求执行，见
  [设计与验收](UI-17-3a-typed-feature-downgrade.md)。17.3b 已将 client/server 事件与协商能力的
  关系收敛到发布合同，Python Bridge 与 Node 共用严格查询和失败校验，见
  [设计与验收](UI-17-3b-event-capability-registry.md)。17.3c 已让 `/doctor export` 在旧 Bridge 上保留
  typed Health 页面、禁止 preview/write 请求并显示明确升级提示，同时证明 Bridge 在 Plan/写盘前失败关闭，见
  [设计与验收](UI-17-3c-doctor-export-downgrade.md)。ARC-03.5a 已补充可协商的 JSONL sequence 检测、
  重复/倒序隔离和缺口后的 TUI fail-closed 回退；ARC-03.3a 已让经过 registry 摘要证明的新增 informational
  事件安全消费 sequence、丢弃 payload 并脱敏审计。未知关键事件局部隔离、cursor 与 snapshot 自动补发仍未完成。
- UI-17.4 Release matrix：OS、Python、Node、终端、安装方式、升级/回滚。
- UI-17.5 Deprecation telemetry：仅本地统计 fallback 原因，不上传用户内容。
- UI-17.6 Release gate：阻断级缺陷、豁免审批和回滚条件。

## 已交付前置

ARC-01.4c1-4c3 已让 New UI 与 TUI 消费同一个 Composition-owned terminal lifecycle factory，并用真实 Harness
SQLite 验证两端 heartbeat/retention/Doctor/terminal 语义。UI-17.1 已建立可机读 manifest，UI-17.2a-17.2f 已锁定
runtime-health、permission/bypass、interaction、基本 run lifecycle、stream/error/retry golden 与双端固定视口
capture；UI-17.3a-17.3c 已交付 Evaluation Lane 与 Doctor Export 的真实 typed feature
downgrade、通用 event-capability registry 和新旧 Bridge 进程证据，其余 golden scenarios 与 UI-17.3 compatibility negotiation
尚未完成，不能凭局部对照通过发布门。

## 验收标准

- parity manifest 中必需项 100% 覆盖；差异有产品理由和测试。
- 新 UI 启动失败 2s 内显示 fallback 命令；fallback 能继续同一 workspace。
- protocol minor 前端兼容，major 不兼容给中文升级提示。
- wheel/binary 安装不下载完整源码；开发安装仍可明确获取源码。
- 三平台 clean install、upgrade、rollback、offline startup 通过。
- 发布后 receipt/trace 可定位使用的是哪个前端和协议版本。
