import { expect, test } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('workspace tree and AI sources use real response data', async ({ page }) => {
  await mockWorkbenchApi(page)
  let retried = ''
  await page.route('**/api/v1/workspace/tree', route => route.fulfill({ json: {
    workspace_root: 'E:/Workspace/NaumiAgent',
    root_id: '.',
    truncated: false,
    max_items: 2500,
    items: {
      '.': { id: '.', name: 'NaumiAgent', path: '.', kind: 'directory', extension: '', children: ['frontend', 'README.md'] },
      frontend: { id: 'frontend', name: 'frontend', path: 'frontend', kind: 'directory', extension: '', children: ['frontend/web2'] },
      'frontend/web2': { id: 'frontend/web2', name: 'web2', path: 'frontend/web2', kind: 'directory', extension: '', children: [] },
      'README.md': { id: 'README.md', name: 'README.md', path: 'README.md', kind: 'file', extension: 'md', children: [] },
    },
  } }))
  await page.route('**/sessions/*/messages?*', route => route.fulfill({ json: {
    total: 2,
    messages: [
      { id: 'question', role: 'user', content: '核对来源', timestamp: '2026-09-11T09:59:00+08:00', metadata: {} },
      { id: 'answer', role: 'assistant', content: '引用已核对。', timestamp: '2026-09-11T10:00:00+08:00', metadata: { citations: [{ id: 'docs', title: '官方文档', url: 'https://example.com/docs', snippet: '这是服务端返回的真实来源摘要。' }] } },
    ],
  } }))
  await page.route('**/sessions/*/messages', route => {
    if (route.request().method() !== 'POST') return route.fallback()
    retried = (route.request().postDataJSON() as { content?: string }).content || ''
    return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n' })
  })

  await page.goto('/web2')
  const assistant = page.locator('.w2-message.assistant')
  await expect(assistant).toContainText('引用已核对。')
  await expect(assistant.getByRole('img', { name: 'NaumiAgent 头像' })).toHaveCount(0)
  const actions = assistant.getByLabel('助手消息操作')
  await expect(actions).toContainText('10:00')
  await expect(actions.getByRole('button')).toHaveCount(4)
  await actions.getByRole('button', { name: '重新生成' }).click()
  await expect.poll(() => retried).toBe('核对来源')
  await page.getByRole('button', { name: '来源' }).click()
  await expect(page.getByRole('link', { name: /官方文档/ })).toHaveAttribute('href', 'https://example.com/docs')
  await page.getByRole('button', { name: '展开来源摘要：官方文档' }).click()
  await expect(page.getByText('这是服务端返回的真实来源摘要。')).toBeVisible()

  await page.getByLabel('工作区面板').getByRole('button', { name: '文件', exact: true }).click()
  const tree = page.getByLabel('工作区文件')
  await expect(tree.getByText('frontend', { exact: true })).toBeVisible()
  await expect(tree.getByText('README.md', { exact: true })).toBeVisible()
  await expect(tree.getByText('web2', { exact: true })).toBeVisible()
  await tree.getByRole('treeitem', { name: /frontend/ }).click()
  await expect(tree.getByText('web2', { exact: true })).toBeHidden()
  await tree.getByRole('treeitem', { name: /frontend/ }).click()
  await expect(tree.getByText('web2', { exact: true })).toBeVisible()
})

test('border beam activates only while the agent is running', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/*/messages', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    await new Promise(resolve => setTimeout(resolve, 500))
    return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n' })
  })
  await page.goto('/web2')
  const beam = page.locator('.community-composer-beam')
  await expect(beam).not.toHaveAttribute('data-active', '')
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('运行边框效果')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(beam).toHaveAttribute('data-active', '')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible({ timeout: 3000 })
  await expect(beam).not.toHaveAttribute('data-active', '')
})
