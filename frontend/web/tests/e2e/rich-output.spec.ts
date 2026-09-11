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
  await expect(page.locator('.rich-content .katex-display .mfrac')).toBeVisible()
  await expect(page.locator('.rich-content table').first()).toBeVisible()
  await page.reload()
  await expect(page.locator('.rich-content .katex').first()).toBeVisible()
  await page.screenshot({ path: '../../.naumi/data/rich-kimi-real.png', fullPage: true })
  const formula = page.locator('.rich-content .katex-display').first()
  await formula.scrollIntoViewIfNeeded()
  await page.evaluate(() => document.fonts.ready)
  await expect(formula.locator('math')).toHaveAttribute('display', 'block')
  await formula.screenshot({ path: '../../.naumi/data/rich-math-fraction.png' })
})

test('interactive charts, tables and isolated HTML work and recover after reload', async ({ page }) => {
  await mockWorkbenchApi(page)
  const table = { type: 'table', title: '销售明细', columns: [{ key: 'name', label: '商品' }, { key: 'value', label: '销售额' }], rows: [{ name: 'A', value: 12 }, { name: 'B', value: -3 }] }
  const spec = { version: 1, type: 'tabs', title: '数据工作台', source: '界面交互测试数据', tabs: [
    { label: '概览', content: { type: 'metrics', title: '指标概览', items: [{ label: '合计', value: 9, unit: '元' }] } },
    { label: '明细', content: table },
    { label: '趋势', content: { type: 'chart', title: '销售趋势', x: 'name', series: [{ key: 'value', label: '销售额' }], rows: table.rows } },
    { label: '组件', content: { type: 'html', title: '交互计数器', html: '<p>计数：<output id="n">0</output></p><button onclick="document.getElementById(\'n\').textContent=Number(document.getElementById(\'n\').textContent)+1">增加</button><button onclick="try { parent.document.body.dataset.compromised=1 } catch(e) { document.getElementById(\'n\').textContent=\'已隔离\' }">检查隔离</button>' } },
  ] }
  await page.route('**/sessions/smoke-session-001/messages*', route => route.fulfill({ json: { messages: [{ role: 'assistant', content: '```naumi\n' + JSON.stringify(spec) + '\n```' }] } }))
  await page.addInitScript(() => localStorage.setItem('naumi:workspace:session', 'smoke-session-001'))
  await page.goto('/web2')
  await expect(page.getByRole('heading', { name: '数据工作台' })).toBeVisible()
  await page.getByRole('tab', { name: '明细' }).click()
  await page.getByRole('textbox', { name: '搜索表格' }).fill('B')
  await expect(page.locator('.rich-content tbody')).toContainText('-3')
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 CSV' }).click()
  expect((await downloadPromise).suggestedFilename()).toBe('data.csv')
  await page.getByRole('tab', { name: '趋势' }).click()
  await page.getByRole('combobox', { name: '图表类型' }).selectOption('line')
  await expect(page.locator('.rich-chart path')).toHaveCount(1)
  await page.getByRole('tab', { name: '组件' }).click()
  await page.getByRole('button', { name: '运行组件' }).click()
  const frame = page.frameLocator('iframe[title="交互计数器"]')
  await frame.getByRole('button', { name: '增加' }).click()
  await expect(frame.locator('output')).toHaveText('1')
  await frame.getByRole('button', { name: '检查隔离' }).click()
  await expect(frame.locator('output')).toHaveText('已隔离')
  expect(await page.evaluate(() => document.body.dataset.compromised)).toBeUndefined()
  await page.getByRole('button', { name: '重置', exact: true }).click()
  await expect(frame.locator('output')).toHaveText('0')
  await page.reload()
  await page.getByRole('tab', { name: '组件' }).click()
  await expect(page.locator('iframe[title="交互计数器"]')).toHaveCount(0)
  await page.getByRole('tab', { name: '趋势' }).click()
  await page.screenshot({ path: '../../.naumi/data/rich-widgets-desktop.png', fullPage: true })
})

test('Kimi generates interactive components from measured repository data', async ({ page }) => {
  const session = process.env.NAUMI_WIDGET_LIVE_SESSION
  test.skip(!session, '需要真实 Kimi 富组件会话')
  await page.addInitScript(id => localStorage.setItem('naumi:workspace:session', id!), session)
  await page.goto('http://127.0.0.1:5174/web2')
  await expect(page.locator('.rich-tablist [role=tab]')).toHaveCount(4)
  const tabs = page.locator('.rich-tablist [role=tab]')
  await tabs.nth(1).click()
  await expect(page.locator('.rich-content table')).toContainText('.py')
  await tabs.nth(2).click()
  await expect(page.locator('.rich-chart')).toBeVisible()
  await tabs.nth(3).click()
  await page.getByRole('button', { name: '运行组件' }).click()
  const frame = page.frameLocator('.rich-sandbox')
  await expect(frame.locator('input[type=range]')).toBeVisible()
  const before = await frame.locator('body').innerText()
  await frame.locator('input[type=range]').focus()
  await frame.locator('input[type=range]').press('ArrowRight')
  expect(await frame.locator('body').innerText()).not.toBe(before)
  await page.screenshot({ path: '../../.naumi/data/rich-kimi-widget-real.png', fullPage: true })
  await page.reload()
  await expect(page.locator('.rich-tablist [role=tab]')).toHaveCount(4)
})

test('real generated images and a CSV load with authenticated asset requests', async ({ page }) => {
  const session = process.env.NAUMI_ASSETS_LIVE_SESSION
  test.skip(!session, '需要真实图片发布会话和资源验证服务')
  await page.addInitScript(id => {
    localStorage.setItem('naumi:workspace:session', id!)
    if (localStorage.getItem('rich-check-no-token')) localStorage.removeItem('naumi:token')
    else localStorage.setItem('naumi:token', 'local-rich-output-verification')
  }, session)
  // Forward to the isolated instance of the real asset API, without restarting the user's daemon.
  await page.route('**/api/v1/output-assets/*', async route => {
    const path = new URL(route.request().url()).pathname
    const response = await route.fetch({ url: `http://127.0.0.1:18766${path}` })
    await route.fulfill({ response })
  })
  await page.goto('http://127.0.0.1:5174/web2')
  const images = page.locator('.rich-image-open img')
  await expect(images).toHaveCount(2)
  for (let i = 0; i < 2; i++) expect(await images.nth(i).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0)
  await page.locator('.rich-image-open').first().click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: '关闭图片' }).click()
  const download = page.waitForEvent('download')
  await page.locator('.rich-file button').click()
  expect((await download).suggestedFilename()).toMatch(/\.csv$/)
  await page.reload()
  await expect(images).toHaveCount(2)
  await page.screenshot({ path: '../../.naumi/data/rich-assets-real.png', fullPage: true })
  await page.evaluate(() => localStorage.setItem('rich-check-no-token', 'true'))
  await page.reload()
  await expect(page.locator('.rich-image-error').first()).toContainText('令牌无效')
})
