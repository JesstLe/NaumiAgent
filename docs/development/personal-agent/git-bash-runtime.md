# Personal Agent 的 Windows Bash 执行环境

日期：2026-11-05

## 真实缺陷

用户环境可以合法指定 Git 安装中的 `usr/bin/bash.exe`，而不是 `bin/bash.exe` 包装入口。
原命令执行通道直接继承父 PATH，同时禁用 profile/rc；当 PATH 没有 Git 的工具目录，
`cat`、`rm`、`sleep` 等实际返回 127。权限 BYPASS 并不能让不存在的命令成功。
本次基线在同一输入下分别复现异步、同步通道退出 127。

## 本次实现

- 根据本次已选择的 Bash 构建子进程 PATH，补入同安装的工具目录，统一 Windows Path 大小写与重复项。
- 不修改父进程环境、用户配置、命令、cwd 或 profile/rc 策略；POSIX 命令及显式环境保持原有合同。
- 异步和同步命令复用同一环境准备；Bash 工具、后台命令、shell hook 与 Skill 动态上下文随调用者复用。
- 本地 `CodeExecuteTool` 的 Bash 脚本不再通过 PATH 误选 WSL；它复用同一解析、环境与进程组。
- 本地 Bash 脚本超时或取消时终止本次进程树；Python/JavaScript、Docker 分支与原工具权限不改变。
- 本地 Bash 脚本按 LF 写入；后台捕获的 CRLF 不再二次转换成 CRCRLF，保留实际输出文本。
- 原 stdout/stderr、真实非零退出码和超时结果保留，不在 Engine 中解析中文字符串猜测成功。

## 验证与边界

新增测试覆盖调用者环境不变、缺失/空 PATH、大小写、重复路径、Unicode/空格、POSIX、
真实同步与异步外部工具、脚本、本地代码执行及错误/超时/取消路径。
既有运行时、Shell 输出、后台、Skill 与权限入口集中回归，原 `remove-me` 失败按原测试验证，
不通过删除 runner 的 shell override 规避。

本次只处理本机 Bash 运行路径，不声明完整电脑操作、多设备或发布验收；
Windows symlink 创建特权是另一项原环境缺项，保留其证据。
所有真实命令只使用独立 fixture 数据，不执行账户动作或模型请求。

命令原文、字面输出、原/改 SHA256、完整源码副本回滚和后改零写入保护：
`.naumi/transactions/git-bash-env-20261105/VERIFICATION.txt`。
