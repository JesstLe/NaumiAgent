# PA-02 搜索框单行布局修正

## 原因与窄修改

实际浏览器截图复查发现，搜索图标居中、输入提示另起一行，控件高度 64px。`.pm-workspace label` 的 `flex-direction:column` 优先级高于原 `.pm-search`，覆盖了搜索框需要的横向布局。

只修改 `frontend/web2/src/personal/memories/memory.css`：

```css
.pm-workspace .pm-search { display:flex; flex-direction:row; min-height:40px; /* existing rules unchanged */ }
.pm-search > svg { flex-shrink:0; }
```

没有改变 React 组件、API、行为、数据或记忆内容。修正前 CSS 保存在 `.naumi/transactions/pa02-20261104/ui-search-before.css`，SHA `d4f95c8fecd8de09ebf4fa75e426ab79a09879bd243e7ae47befac422d5f09ef`。

## 一次全前端验证

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-full-frontend-search -- D:/DEV/pnpm/pnpm.exe --dir frontend/web exec vitest run
```

stdin 为空。实际输出：`Test Files 30 passed (30)`、`Tests 311 passed (311)`、`RECORDED_EXIT=0`。这是前端 Vitest 配置下的完整 web/shared 测试，不是先前 8 文件子集；之前的 183 项证据仍保留，不相加计数。日志有 jsdom 未安装 canvas 的 getContext 诊断，未产生失败。

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-build-search -- D:/DEV/pnpm/pnpm.exe --dir frontend/web run build
```

stdin 为空。`tsc -b` 完成；4545 modules transformed；`✓ built in 1.10s`；`RECORDED_EXIT=0`。既有大 bundle 提示仍在，未做额外拆包改动。

## 真实浏览器只读复查

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-search-readonly -- .venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_search_readonly.py
```

复用原 persistent browser profile 和真实服务器；只查看已有 `pm_c6b6005c2c302d433db85cfcfb0356f1`、revision 3、deleted tombstone。所有非 GET/HEAD/OPTIONS 请求均阻断；实际 mutation 数 0、page error 数 0、provider 执行 0，没有为截图新建记忆。

| Viewport | 方向 | 控件高度 | 图标中心 Y | 输入中心 Y | 文档宽 |
| --- | --- | --- | --- | --- | --- |
| 1440 × 1000 | row | 40px | 275 | 275 | 1440 |
| 390 × 844 | row | 40px | 307.5 | 307.5 | 390 |

实际输出：`PA02_SEARCH_VISUAL=PASS; MUTATIONS=0; PROVIDER_EXECUTION=0`，exit 0。两张图片均已重新打开并目视确认图标在左、输入同行、没有横向溢出：

- `.naumi/transactions/pa02-20261104/ui-search-desktop-1440.png`，SHA `156614ad6b479fff547365c240da5cc8d91d4e328e6ea85a54e13bd35e27ef25`。
- `.naumi/transactions/pa02-20261104/ui-search-narrow-390.png`，SHA `47f53d5372db62d2a42b191c6e76e81f53ec5b7a40eebda0feb11e20dd3e9a05`。

原业务闭环验收、所有失败 attempt、旧截图和旧 183 项记录均保留。当前 UI manifest 更新为 `ui-files.json`；更新前 manifest 另存 `ui-files-before-search.json`；本次修改和验证文件由 `ui-search-files.json` 记录。
