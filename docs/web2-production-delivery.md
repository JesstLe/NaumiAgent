# Web2 生产式前端交付

## 原因

Vite 开发服务器会直接向浏览器提供 TSX 模块，以支持热更新和源码定位。因此
通过 `pnpm dev` 启动的 5174 页面会在开发者工具中列出 `web2/src`。这是开发模式
行为，不是生产构建意外生成了 source map。

## 处理

- Vite 生产构建明确使用 Oxc 压缩并关闭 source map。
- 新增 `pnpm serve:production`，先构建 `web/dist`，再在 5174 提供生产资源。
- 模型密钥、文件权限和工具执行继续位于 Python／Tauri 后端；前端包不保存密钥。

生产页面只返回哈希命名的 JS/CSS 资源。浏览器请求 `/@vite/client`、`/@fs/...tsx`
或 `/src/main.tsx` 时只会命中单页应用的 HTML 回退，不会得到 TSX 模块内容。

## 边界

浏览器必须下载前端 JavaScript 和 CSS 才能显示界面，所以用户仍可检查 DOM、计算
样式和压缩后的脚本。关闭 source map 能避免直接交付原始 TSX 文件和源码目录结构，
但不能把浏览器代码变成不可分析的秘密代码。需要保密的算法与数据必须放在后端。
