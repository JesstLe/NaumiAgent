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

test('sent user messages can be edited and replace the following conversation branch', async ({ page }) => {
  let edited = false
  let submitted: Record<string, unknown> = {}
  await page.route('**/sessions/*/runs*', route => route.fulfill({
    json: {
      runs: [{
        id: edited ? 'edited-run' : 'old-run',
        user_message_id: edited ? 'legacy-edited' : 'legacy-old',
        status: 'completed',
        started_at: '2026-09-11T00:00:00Z',
        completed_at: '2026-09-11T00:00:03Z',
        steps: [{
          sequence: 1,
          stage: 'request',
          status: 'completed',
          summary: edited ? '修改后的问题' : '原始问题',
          detail: '',
        }],
      }],
      total: 1,
    },
  }))
  await page.route('**/sessions/*/messages*', async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON() as Record<string, unknown>
      edited = true
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: {"id":"token","type":"token_delta","run_id":"edited-run","data":{"token":"修改后的回答"}}\n\ndata: {"id":"end","type":"agent_end","run_id":"edited-run","data":{"status":"completed"}}\n\n',
      })
    }
    return route.fulfill({ json: edited ? {
      messages: [
        { id: 'question', role: 'user', content: '修改后的问题', timestamp: '', metadata: {} },
        { id: 'edited-answer', role: 'assistant', content: '修改后的回答', timestamp: '', metadata: {} },
      ],
      total: 2,
    } : {
      messages: [
        { id: 'question', role: 'user', content: '原始问题', timestamp: '', metadata: {} },
        { id: 'answer', role: 'assistant', content: '旧回答', timestamp: '', metadata: {} },
        { id: 'later-question', role: 'user', content: '后续问题', timestamp: '', metadata: {} },
        { id: 'later-answer', role: 'assistant', content: '后续回答', timestamp: '', metadata: {} },
      ],
      total: 4,
    } })
  })

  await page.goto('/web2')
  const conversation = page.getByLabel('对话', { exact: true })
  await expect(conversation.getByText('原始问题', { exact: true })).toBeVisible()
  await conversation.getByRole('article').filter({ hasText: '原始问题' }).hover()
  await page.getByRole('button', { name: '编辑消息' }).first().click()
  await page.getByRole('textbox', { name: '编辑已发送消息' }).fill('修改后的问题')
  await page.getByRole('button', { name: '发送修改' }).click()

  await expect(page.getByText('修改后的回答', { exact: true })).toBeVisible()
  await expect(page.getByText('旧回答', { exact: true })).toHaveCount(0)
  await expect(page.getByText('后续问题', { exact: true })).toHaveCount(0)
  expect(submitted).toMatchObject({ content: '修改后的问题', edit_message_id: 'question' })

  await page.reload()
  await expect(conversation.getByText('修改后的问题', { exact: true })).toBeVisible()
  await expect(conversation.getByText('原始问题', { exact: true })).toHaveCount(0)
  await expect(conversation.getByText('旧回答', { exact: true })).toHaveCount(0)
})

test('assistant actions stay hidden until the related run is terminal', async ({ page }) => {
  let terminal = false
  await page.route('**/sessions/*/messages*', route => route.fulfill({ json: {
    messages: [
      { id: 'question', role: 'user', content: '运行中的问题', timestamp: '', metadata: {} },
      { id: 'answer', role: 'assistant', content: '已经输出的部分正文', timestamp: '', metadata: {} },
    ],
    total: 2,
  } }))
  await page.route('**/sessions/*/runs*', route => route.fulfill({ json: {
    runs: [{
      id: 'run-1',
      user_message_id: 'question',
      status: terminal ? 'completed' : 'running',
      started_at: '2026-09-11T00:00:00Z',
      completed_at: terminal ? '2026-09-11T00:00:03Z' : '',
      steps: [{ sequence: 1, stage: 'request', status: 'completed', summary: '运行中的问题', detail: '' }],
    }],
    total: 1,
  } }))

  await page.goto('/web2')
  await expect(page.getByText('已经输出的部分正文', { exact: true })).toBeVisible()
  await expect(page.locator('.community-message-action-bar')).toHaveCount(0)

  terminal = true
  await page.reload()
  await expect(page.locator('.community-message-action-bar')).toBeVisible()
})
