# PA-08：资料库与版本化 Page 界面

日期：2026-11-04

本记录描述真实 API 驱动的前端实现。源码基线为 `ae1f2c9a74b5e73d513fba64e51f8f3a8e145270`。
本文分别保留前端单元、受控视觉和真实 HTTP/浏览器证据。后端权限、宿主进程隔离、真实模型、
冷重启和整模块副本回滚由 PA-08 主任务独立验收；单元测试与 UI fixture 不替代这些证据。

## 1. 实现范围

- 导航新增“资料库”，使用主内容区，不将独立文档挤进右侧窄面板。
- 当前主 Agent、已有 Agent 和团队可通过身份选择器分别打开资料库。
  手动 API 使用认证用户选择的 `actor_id`；界面不创建任何默认角色或修改用户自取名字。
- 目录读取真实分页接口，分类、搜索、翻页会改变实际查询参数。
- 每张卡片绑定 `artifact_id/version/sha256`，打开具体版本；不将受赠 v1 自动替换成无权读取的 v2。
- URL 保留 `library/actor/artifact/version`，重新打开不依赖原聊天仍存在，也不把令牌写进 URL。
- 保留原对话组件挂载，打开资料库仅隐藏原对话/终端区，不清空草稿、附件或中断后台工作。
- 对话 `metadata.artifact_refs` 和资料 `metadata.artifact_ref` 打开同一个版本化 Page。

## 2. Page 与实际数据操作

- 文档目录跳转到真实章节；章节保留作者的事实、估算、未知标注，不把它提升成独立核验。
- 来源卡读取该来源的确切版本和摘要；读取失效时保留待核对状态，不显示假来源标题。
- CSV 预览复用 `RichWidget` 的真实搜索、数值排序和筛选结果导出；公式前缀进行 CSV 转义。
  超过预览范围时明确区分预览行数、筛选导出和顶部完整原文件下载。
- Page 的 Markdown 不自动加载远程图片，也不退回公开哈希资源 URL。
  未绑定资料版本的 image widget 不自动加载；HTML widget 保留原 CSP 与 iframe sandbox。
- 私有图片和 PDF 使用带认证的内容接口，核对响应 SHA、文件字节数与实际 SHA 后创建 object URL，
  关闭或切换版本时撤销 URL；SVG 不插入 `innerHTML`。
- 下载重新从内容接口获取原版本并校验完整字节，不从截取预览拼造下载文件。

## 3. 修订与原请求身份

- 文本和结构化 Page 编辑生成新版本，使用实际 `expected_version` CAS；旧版本保持可读。
- Page 编辑支持章节标题、正文、证据标注、排序和组件定义；无效组件 JSON 保留原输入并阻止保存，
  不悄悄保存上一次有效定义。
- 空章节提供“添加表格”入口，生成可编辑的真实表格 schema；Library 对复用表格提供局部宽度、
  单元格间距和表头样式，不依赖聊天容器或改变全局表格。
- 图片、PDF 和输入材料通过 multipart 二进制修订入口上传新版本，不把二进制编码成文本。
- 保存、上传、分享和撤销均先保留稳定原请求。网络结果不确定时原样重试，不通过错误文字推断未受理。
- 上传记录绑定原文件名、类型、字节数和内容 SHA；刷新后重试需重新选择相同原文件，
  不因文件对象不在内存就创建新的请求编号。
- 浏览器存储无法保存请求身份时不发送变更请求；无法解析的已有原请求不会被覆盖成新请求。
- 已受理凭据可打开已保存版本，但仍保留原请求，直到原样重放取得匹配结果。
- 放弃重试有确认，而且不表示撤销已创建的版本或分享。
- 目录读取错误与变更错误分别保留，晚到的目录成功不会清除保存失败或未知结果。

## 4. 分享与编辑保护

- 分享对象来自用户实际已有的 Agent/团队目录，不提供职业模板或预置名字。
- 分享绑定所选版本；撤销绑定原 grant ID/revision，并有明确确认。
- 未保存编辑在关闭、切换资料库身份、离开资料库及浏览器历史导航前要求决策。
  取消导航时保留编辑；已经确认保存的版本由唯一受理回调打开，不重复写历史记录或弹“放弃修改”。
- 页面卸载提示不等于自动保存草稿；请求尚未发送前，没有伪造服务端版本。

## 5. Agent 概念修正

用户明确要求这些对象是通用 Agent，不应被职业“专员”概念限制。

- 泛称统一为“我的 Agent”“主 Agent”“创建/编辑 Agent”“Agent 消息/互询”。
- 内部 `kind=specialist` 暂保兼容，界面选项显示 `Agent`，团队仍显示“团队”。
- 没有迁移或重命名已有档案、ID、历史消息和版本。
- 新增自由命名验证：`Lambda-7`、`Mira 🌙`、`山海` 原样提交与显示，不自动追加“员”等后缀。

## 6. 前端验证

事务目录：`.naumi/transactions/pa08-20261104/`。

- 原始前端：25 files / 213 passed，退出 0。
- 最终前端功能批次：**27 files / 255 passed**，`Duration 9.21s`，退出 0。
- 该批次 TypeScript 与生产构建通过，`built in 734ms`，退出 0。
- 随后仅修正 Library 表格 CSS，再执行 TypeScript 与生产构建，`built in 669ms`，退出 0；
  此次没有重复运行功能测试，没有改动 TS/TSX 或测试文件。
- `git diff --check -- frontend` 退出 0。
- 功能批次精确命令、空 stdin、完整 stdout/stderr、退出状态见 `ui-final-table-fixed-record.json`；
  最终 CSS 构建见 `ui-final-visual-build-record.json`。
- 10 份原始前端文件字节和 SHA 保存在 `original/` 与 `ui-baseline-manifest.json`；新增文件无原始版本。
- 单元测试覆盖：稳定请求与跨重载重试、不同服务器/工作区/Agent 隔离、并发点击、存储损坏/配额错误、
  文件 SHA 重选、二进制 CAS、独立链接、版本范围、来源、私有渲染、真实表格导出、分享撤销、
  旧身份晚返回、私有 URL 释放、未知原请求保留、晚到读取不清写失败、新建表格入口以及未保存编辑导航保护。

首次模块批次的 3 个失败保留在 `ui-first-tests.txt`：两个测试重用了已消费的 Response，
一个旧互询测试遗漏更新新 Agent 标签，均已修正。
中间记录器曾在打印已完成的构建输出时触发 GBK 编码错误；原测试和构建结果均已落盘，
修正输出编码后仅补执行未完成的 diff-check，没有重复运行那两条命令。
最终导航保护批次使用独立记录，没有覆盖前一批证据。

既有 jsdom canvas 提示和 Vite 大 chunk 提示保留原日志，不作为通过范围外的修复声明。

### 受控视觉检查

- 使用实际生产模式前端组件和 Chrome，无需启动额外服务器。
- 数据由隔离 UI fixture 提供，明确记录 `mocks=true/backend=false/model=false`，不作为真实 HTTP 或模型验收。
- 1440 / 768 / 390 三种宽度分别检查资料目录、Page 和版本分享，共 9 张实际 PNG。
- 浏览器异常 0、页面横向溢出 0；所有 PNG 已打开检查。
- `ui-visual-report.json`、`ui-visual-log.txt` 和 `ui-{library,page,share}-fixture-{1440,768,390}.png`
  保存在同一事务目录；HTML 和构建产物仅作为视觉测试材料，不进入产品代码。

### 真实 HTTP 与浏览器验收

- 服务：`http://127.0.0.1:18769`，使用当前生产 build、实际 API 和隔离测试数据；无 API mock，
  无聊天消息 POST，浏览器异常 0。详见 `browser-result.json` 与 `browser-stage2-resume2-log.txt`。
- 从界面显式创建 `Mira 🌙`、`山海`，名字原样保存；没有预置职业 Agent。
- 从实际工作区上传 `budget.csv`，读取收入 1200、支出 375，创建绑定材料确切 v1/SHA 的 Page。
- 表格实际数值升序为 375 → 825 → 1200；搜索“结余”后实际导出一行 825，下载的原 JSON 与
  确切版本 SHA 匹配；搜索导出不是完整原文件下载。
- Page v1 分享给山海后，Mira 创建 v2；山海仍只见 v1，v2 HTTP 拒绝。撤销旧 grant 后，
  已开链接和内容接口拒绝且 `no-store`；显式重新分享产生新 grant，旧 grant 保持 revoked。
- 使用旧 grant 创建衍生内容返回真实 `404 not_found`，未复活来源；使用新 grant 从实际 UI
  创建“山海接收确认”成功，产物绑定新 grant 身份。
- 同一个 SVG 资料从真实上传 v1 修订为二进制 v2；Chrome 实际显示黑色圆形，原文件下载字节、
  内容 SHA、响应 SHA 和 `no-store` 均匹配。
- `browser_stage2.mjs` 和 `browser_stage2_resume.mjs` 曾因重名 locator 停止；失败材料保留，
  续跑先核对原 Page/request/grant 后从未完成阶段继续，没有重复创建或分享。
- 最终只读视觉复查脚本 `browser_visual_final.mjs` 重新打开既有资料并等待分享清单实际加载；
  任何非读取 API 会被 guard 阻止，实际 `API_WRITES=0`，没有再次变更资料或 grant。
- 最终 1440 / 768 / 390 各检查目录、Page、分享，再检查 1440 SVG，共 **10 张实际 PNG**；
  页面横向溢出 0，表格有完整列宽和 14px 水平单元格间距，分享显示旧 grant 已撤销及新 grant 可访问。
- 最终真实截图为 `browser-{library,page,share}-final-{1440,768,390}.png`、
  `browser-svg-final-1440.png`；10 张均已通过 `view_image` 逐张打开检查。
  `browser-visual-final-report.json` 与 `browser-visual-final-log.txt` 保留结果，早期截图未覆盖。

关键实际对象：

| 对象 | ID / 终态 |
|---|---|
| Mira 🌙 | `pa_8dd99f9009994b69a45f2bdaa1dc7d2c` |
| 山海 | `pa_3fb8855c98454576b9fe639cd005e8c8` |
| budget.csv | `art_d1d0da35af3e4527be4260fd30053311` v1 |
| 浏览器预算核验 Page | `art_c2109a760a104f4196264a7308b2f548` v1/v2 |
| 旧 Page v1 grant | `asg_401e78df7a65468280c7a8f72cd7bcbc` revoked |
| 新 Page v1 grant | `asg_3fb6d71c882043d0bff76dcf10c0b2a5` active |
| 山海接收确认 | `art_fe4834b05aec4306a73a2918d27b22bb` v1 |
| SVG 二进制修订 | `art_d9e927a8a27a4bb0989e0a3bd4276491` v1/v2 |

## 7. 主任务继续核验

本子任务已完成真实后端上传、版本修订、分享/撤销、完整内容下载、重新打开、移动宽度及上述读取负向场景。
真实模型、完整宿主隔离、冷重启、全模块回归与统一源码回滚由根任务继续收口。
未提交 Git，未重启或停止根任务的验收服务；自有 Chrome 上下文已关闭。
当前文档不宣称整个 PA-08 或 24 项目标完成。
