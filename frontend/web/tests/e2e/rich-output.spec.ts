import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

const markdown = '## 分析结论\n\n**已核对**，支持公式 $E=mc^2$。\n\n| 项目 | 数量 |\n| --- | --- |\n| 文件 | 12 |\n\n- [x] 读取数据\n\n$$\\sum_{i=1}^{n}x_i$$\n\n```python\nprint(42)\n```'

test('Markdown and LaTeX render after loading, reloading and on a narrow viewport', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/smoke-session-001', route => route.fulfill({ json: {
    id: 'smoke-session-001', title: '富内容验收', model: 'openai/kimi-for-coding', status: 'active',
    messages: [{ role: 'user', content: '分析数据' }, { role: 'assistant', content: markdown }],
  } }))
  await page.route('**/sessions/smoke-session-001/messages*', route => route.fulfill({ json: { messages: [{ role: 'user', content: '分析数据' }, { role: 'assistant', content: markdown }] } }))
  await page.addInitScript(() => localStorage.setItem('naumi:workspace:session', 'smoke-session-001'))
  await page.goto('/web2')
  for (const reload of [false, true]) {
    if (reload) await page.reload()
    await expect(page.getByRole('heading', { name: '分析结论' })).toBeVisible()
    await expect(page.locator('.katex').first()).toBeVisible()
    await expect(page.locator('.rich-content table')).toContainText('文件')
  }
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../../.naumi/data/rich-markdown-mobile.png', fullPage: true })
})

test('real Kimi response renders from SQLite through the running backend', async ({ page }) => {
  const session = process.env.NAUMI_RICH_LIVE_SESSION
  test.skip(!session, '需要真实 Kimi 消息会话')
  await page.addInitScript(id => localStorage.setItem('naumi:workspace:session', id!), session)
  await page.goto('http://127.0.0.1:5174/web2')
  await expect(page.locator('.rich-content .katex').first()).toBeVisible()
  await expect(page.locator('.rich-content table').first()).toBeVisible()
  await page.reload()
  await expect(page.locator('.rich-content .katex').first()).toBeVisible()
  await page.screenshot({ path: '../../.naumi/data/rich-kimi-real.png', fullPage: true })
})
