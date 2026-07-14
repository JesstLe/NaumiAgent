# CC-02 React/Ink Renderer 可替换性实验

## 决策问题

保持现有轻量 Node renderer，还是在稳定 JSONL 协议后引入 React/Ink？本模块只做可测实验，
不直接替换默认 UI。

## 子模块

- CC-02.1 Adapter：同一 `protocol-contract.json` 驱动 current/Ink 两个 renderer。
- CC-02.2 Core views：conversation、tool、permission、footer、task 五个代表组件。
- CC-02.3 Input：multiline、IME、paste、key parsing、permission modal 优先级。
- CC-02.4 Performance：1k cards、token burst、resize、scroll、memory、startup。
- CC-02.5 Packaging：Node version、依赖体积、wheel/binary、offline install。
- CC-02.6 Decision record：量化收益、缺陷、迁移成本和回退路径。

## 决策门槛

只有同时满足才允许进入替换计划：

- 所有 UI-17 必需语义通过同一 fixture；
- 输入/滚动/首帧性能不比当前 renderer 退化 15% 以上；
- 安装体积和启动时间在发布预算内；
- Windows/macOS/Linux 至少各一真实终端通过；
- current renderer 保留为一个稳定版本的回退。

## 验收

实验必须产出 benchmark JSON、截图/录屏、失败列表和明确 `adopt|defer|reject`，不得以主观
“更像 Claude Code”作为结论。
