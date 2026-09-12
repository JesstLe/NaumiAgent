# 首次启动运行时恢复

## 问题

`0.1.214` 在加入图片发布能力后开始依赖 Pillow，但版本号没有同步递增。已经通过
`uv tool install --editable` 安装过同版本的环境仍保留旧依赖元数据，导致 Node 终端在
Python bridge 初始化时因 `PIL` 缺失退出；随后 Textual 回退复用同一后端，再次报出相同错误。

首次引导还有一处发行布局问题：它只按源码仓目录查找 `frontend/terminal-ui`，并要求执行
`npm install`。正式终端入口只引用 Node 标准库和随 wheel 携带的本地模块，`ink`、`react`
只服务于实验渲染器和基准测试，产品启动不需要下载 npm 依赖。

## 实现

- 版本递增到 `0.1.215`，让 `uv tool` 升级时重新解析并安装 Pillow；
- 首次引导同时支持源码仓和 wheel 内的终端资源目录；
- 首次引导以 `node --check` 校验真实入口，不再写入源码目录或 site-packages，也不再安装
  产品运行不需要的 npm 依赖；
- Node 与 Textual 启动前共同检查 Pillow。若安装环境残缺，直接给出一次可执行的恢复命令，
  避免先打开 Node UI、退出、再让 Textual 重复失败。
- provider 专属系统凭据优先于通用 `NAUMI_MODELS__API_KEY`，避免另一提供商的环境变量覆盖
  已验证凭据；首次引导也会先发现已保存凭据，并通过 provider 的 `/models` 接口验证后再写入。
- 认证失败文案不再把“服务拒绝凭据”笼统描述成“未设置”，改为提示核对 provider 并运行
  `naumi configure`，Windows 用户不会再看到仅适用于 Unix shell 的 `export` 命令。

## 验证范围

本模块只运行 onboarding、终端 launcher、wheel 元数据与真实本机 `uv tool` 启动链路。
不在本次修复中执行仓库全量测试。
