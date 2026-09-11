import { test, expect } from '@playwright/test'
import { mockWorkbenchApi, snapshot } from './mocks'
const todos = [
  { id: 'a', subject: '读取项目配置', status: 'completed', blocked_by: [], active_form: null },
  { id: 'b', subject: '核对配置约束', status: 'pending', blocked_by: ['a'], active_form: null },
]
test('context cards retain official foundation and refresh errors recover', async ({ page }) => {
  await mockWorkbenchApi(page)
  let fail = false
  await page.route('**/sessions/*/snapshot', async route => {
    if (fail) return route.fulfill({ status: 503, json: { detail: '上下文服务暂不可用' } })
    return route.fulfill({ json: { ...snapshot, worktrees: [], context_snapshots: [{ id: 'c', health: 'good', task_id: 'a', agent_id: 'reader', reasons: ['已核对项目配置与当前约束'], created_at: '2026-09-11T01:00:00Z' }], validation_runs: [] } })
  })
  await page.goto('/web2')
  await page.getByRole('button', { name: '上下文', exact: true }).click()
  const panel = page.getByLabel('工作区面板')
  const card = panel.locator('.rounded-card')
  await expect(card).toContainText('已核对项目配置')
  await expect(card).toHaveCSS('border-radius', '10px')
  await expect(card).toHaveCSS('background-color', 'oklch(1 0 0)')
  await expect(card.locator('.primitive-card-bar')).toHaveCSS('padding', '10px 12px')
  const shadow = await card.evaluate(node => getComputedStyle(node).boxShadow)
  expect(shadow).toContain('18px 47px')
  await expect(card.locator('.shadow-btn')).toHaveCSS('transition-timing-function', 'cubic-bezier(0.23, 1, 0.32, 1)')
  fail = true
  await panel.getByRole('button', { name: '刷新上下文快照' }).click()
  await expect(panel.getByRole('alert')).toContainText('后端服务错误')
  await expect(card).toBeVisible()
  fail = false
  await panel.getByRole('button', { name: '刷新上下文快照' }).click()
  await expect(panel.getByRole('alert')).toHaveCount(0)
})
test('dependency dragging moves its connector and insights inspect actual status groups', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/*/todos', route => route.fulfill({ json: { todos } }))
  await page.goto('/web2')
  const panel = page.getByLabel('工作区面板')
  await panel.getByRole('button', { name: '依赖', exact: true }).click()
  const graph = panel.getByLabel('任务依赖图')
  await expect(graph).toContainText('2 个节点 · 1 条连接')
  const connector = graph.locator('svg > path[stroke-width="1.25"]')
  const before = await connector.getAttribute('d')
  const node = graph.getByRole('button', { name: /读取项目配置/ })
  const bounds = (await node.boundingBox())!
  await page.mouse.move(bounds.x + 50, bounds.y + 20)
  await page.mouse.down()
  await page.mouse.move(bounds.x + 65, bounds.y + 40, { steps: 5 })
  await page.mouse.up()
  await expect(connector).not.toHaveAttribute('d', before!)
  await panel.getByRole('button', { name: '洞察', exact: true }).click()
  const insights = panel.getByLabel('会话洞察')
  await expect(insights).toContainText('2 条任务状态记录')
  await insights.getByRole('button', { name: '待处理: 50%' }).click()
  await expect(insights.getByRole('button', { name: '待处理: 50%' })).toHaveAttribute('aria-pressed', 'true')
  await insights.getByRole('button', { name: '查看待办' }).click()
  await expect(panel.getByLabel('待办状态 核对配置约束')).toHaveValue('pending')
})
test('empty summaries do not display upstream sample data', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/*/todos', route => route.fulfill({ json: { todos: [] } }))
  await page.goto('/web2')
  const panel = page.getByLabel('工作区面板')
  await panel.getByRole('button', { name: '上下文', exact: true }).click()
  await expect(panel).toContainText('当前会话暂无上下文快照')
  await panel.getByRole('button', { name: '依赖', exact: true }).click()
  await expect(panel).toContainText('当前会话暂无待办依赖')
  await panel.getByRole('button', { name: '洞察', exact: true }).click()
  await expect(panel).toContainText('当前会话暂无可统计记录')
  await expect(panel).not.toContainText('Vanilla')
})
