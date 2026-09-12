import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('todos persist across reload and report failed updates', async ({ page }) => {
  await mockWorkbenchApi(page)
  const todos: {id: string; subject: string; status: string; blocked_by: string[]}[] = []
  await page.route('**/sessions/*/todos', route => {
    if (route.request().method() === 'POST') todos.push({ id: '1', status: 'pending', ...route.request().postDataJSON() })
    return route.fulfill({ json: { todos } })
  })
  await page.route('**/sessions/*/todos/*', route => route.fulfill({ status: 409, json: { detail: '请先完成依赖的待办' } }))
  await page.goto('/web2')
  await page.getByRole('button', { name: '置顶摘要' }).click()
  await page.getByLabel('新增待办').fill('检查配置文件')
  await page.getByRole('button', { name: '添加待办', exact: true }).click()
  await expect(page.getByLabel('待办状态 检查配置文件')).toHaveValue('pending')
  await page.getByLabel('待办状态 检查配置文件').selectOption('in_progress')
  await expect(page.getByRole('alert')).toContainText('请先完成依赖')
  await expect(page.getByLabel('待办状态 检查配置文件')).toHaveValue('pending')
  await page.reload()
  await page.getByRole('button', { name: '置顶摘要' }).click()
  await expect(page.getByLabel('待办状态 检查配置文件')).toHaveValue('pending')
})

test('completed todos leave the active list and remain available as history', async ({ page }) => {
  await mockWorkbenchApi(page)
  const todos = [
    { id: '1', subject: '检查页面', description: '', status: 'completed', active_form: null, blocked_by: [], updated_at: '' },
    { id: '2', subject: '提交结果', description: '', status: 'completed', active_form: null, blocked_by: [], updated_at: '' },
  ]
  await page.route('**/sessions/*/todos', route => route.fulfill({ json: { todos } }))
  await page.goto('/web2')
  await page.getByRole('button', { name: '置顶摘要' }).click()
  const summary = page.getByRole('region', { name: '待办摘要' })

  await expect(summary).toContainText('本轮待办已全部完成')
  await expect(summary).toContainText('2 项已从活跃列表收起')
  await expect(summary.getByLabel('待办完成进度')).toHaveCount(0)
  await expect(summary.getByLabel('待办状态 检查页面')).toHaveCount(0)

  await summary.getByRole('button', { name: '已完成 2 项' }).click()
  await expect(summary.getByLabel('待办状态 检查页面')).toBeVisible()
  await expect(summary.getByLabel('待办状态 提交结果')).toBeVisible()
})
