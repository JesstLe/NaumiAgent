import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test.beforeEach(async ({ page }) => {
  await mockWorkbenchApi(page)
})

test('shared draft and stream survive changing the presentation shell', async ({
  page,
}) => {
  await page.goto('/web2')
  await expect(
    page.getByRole('button', { name: '冒烟测试会话', exact: true }),
  ).toBeVisible()
  await page
    .getByRole('textbox', { name: '消息', exact: true })
    .fill('两个界面共享同一条草稿')
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await page.getByRole('link', { name: '打开原版 Web' }).click()
  await expect(page.getByPlaceholder('输入问题或指令...')).toHaveValue(
    '两个界面共享同一条草稿',
  )
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(
    page.getByText('共享工作区测试回复', { exact: true }),
  ).toBeVisible()
})

test('review, keyboard controls, file empty state and draft reload', async ({
  page,
}) => {
  await page.goto('/web2')
  await expect(
    page.getByRole('button', { name: '冒烟测试会话', exact: true }),
  ).toBeVisible()
  await page.getByLabel('工作区面板').getByRole('button', { name: '审查', exact: true }).click()
  await expect(page.getByText('src/main.py', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '返回工作区' }).click()
  await page.getByLabel('工作区面板').getByRole('button', { name: '文件', exact: true }).click()
  await expect(page.getByText('还没有添加文件')).toBeVisible()
  await page
    .getByRole('textbox', { name: '消息', exact: true })
    .fill('保留的草稿')
  await page.reload()
  await expect(
    page.getByRole('textbox', { name: '消息', exact: true }),
  ).toHaveValue('保留的草稿')
  await page.keyboard.press('Control+j')
  await expect(page.getByRole('region', { name: '终端执行记录' })).toBeHidden()
  await page.keyboard.press('Control+k')
  await page.getByRole('textbox', { name: '搜索历史会话' }).fill('不存在的会话')
  await expect(page.getByText('没有匹配的会话')).toBeVisible()
})

for (const width of [390, 768, 1280, 1920]) {
  test(`responsive workspace fits ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 })
    await page.goto('/web2')
    await expect(
      page.getByRole('textbox', { name: '消息', exact: true }),
    ).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    )
    expect(overflow).toBe(false)
    const composer = await page
      .getByRole('textbox', { name: '消息', exact: true })
      .boundingBox()
    expect(composer!.x).toBeGreaterThanOrEqual(0)
    expect(composer!.x + composer!.width).toBeLessThanOrEqual(width)
    if (width < 821) {
      await page.getByRole('button', { name: '打开文件面板' }).click()
      await expect(page.getByText('会话文件', { exact: true })).toBeVisible()
    }
  })
}

test('offline shell preserves input and exposes recovery settings', async ({
  page,
}) => {
  await page.route('**/workbench/daemon/status', (route) => route.abort())
  await page.goto('/web2')
  await expect(
    page.getByRole('button', { name: '重新连接', exact: true }),
  ).toBeVisible()
  await page
    .getByRole('textbox', { name: '消息', exact: true })
    .fill('离线草稿')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled()
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await expect(page.getByLabel('API 地址')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(
    page.getByRole('textbox', { name: '消息', exact: true }),
  ).toHaveValue('离线草稿')
})

test('keeps pinned summary separate from right-panel context and navigation active during a run', async ({ page }) => {
  await page.route('**/sessions/*/messages', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    await new Promise(resolve => setTimeout(resolve, 1200))
    return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n' })
  })
  await page.goto('/web2')
  await expect(page.locator('.w2-chat-summary')).toHaveCount(0)
  await page.getByRole('button', { name: '置顶摘要' }).click()
  await expect(page.getByRole('region', { name: '待办摘要' })).toBeVisible()
  await page.getByRole('button', { name: '关闭置顶摘要' }).click()
  await page.getByRole('button', { name: '上下文', exact: true }).click()
  await expect(page.getByText('当前会话暂无上下文快照')).toBeVisible()

  await page.getByRole('textbox', { name: '消息', exact: true }).fill('运行时仍能导航')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByRole('button', { name: '停止执行' })).toBeVisible()
  await page.getByRole('button', { name: '插件', exact: true }).first().click()
  await expect(page.getByText('demo-skill', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '搜索会话' }).click()
  await expect(page.getByRole('textbox', { name: '搜索历史会话' })).toBeEnabled()
})

test('session menu exposes durable conversation actions', async ({ page }) => {
  await page.goto('/web2')
  await page.getByRole('button', { name: '会话操作 冒烟测试会话' }).click()
  await expect(page.getByRole('button', { name: '修改名称' })).toBeVisible()
  await expect(page.getByRole('button', { name: '置顶', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '复制会话' })).toBeVisible()
  await expect(page.getByRole('button', { name: '归档' })).toBeVisible()
  await page.getByRole('button', { name: '修改名称' }).click()
  await page.getByLabel('会话名称').fill('新的会话名称')
  await page.getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('button', { name: '新的会话名称', exact: true })).toBeVisible()
})
