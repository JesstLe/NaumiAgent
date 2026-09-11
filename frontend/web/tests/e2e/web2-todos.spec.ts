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
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '待办', exact: true }).click()
  await page.getByLabel('新增待办').fill('检查配置文件')
  await page.getByRole('button', { name: '添加待办', exact: true }).click()
  await expect(page.getByLabel('待办状态 检查配置文件')).toHaveValue('pending')
  await page.getByLabel('待办状态 检查配置文件').selectOption('in_progress')
  await expect(page.getByRole('alert')).toContainText('请先完成依赖')
  await expect(page.getByLabel('待办状态 检查配置文件')).toHaveValue('pending')
  await page.reload()
  await expect(page.getByText('检查配置文件', { exact: true })).toBeVisible()
})
