import { expect, test } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('slash completion uses command API and preserves rejected input', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/api/v1/commands', route => route.fulfill({ json: { commands: [
    { command: '/version', description: '查看版本', aliases: ['/v'], readonly: true, arguments: { syntax: '', required: false } },
  ] } }))
  let command = ''
  await page.route('**/sessions/*/commands', route => {
    command = route.request().postDataJSON().command
    return route.fulfill({ status: 422, json: { detail: '命令暂不可用' } })
  })
  await page.goto('/web2')
  await expect(page.getByRole('button', { name: '冒烟测试会话', exact: true })).toBeVisible()
  await expect(page.getByText('正在加载会话…')).toHaveCount(0)
  const composer = page.getByRole('textbox', { name: '消息', exact: true })
  await composer.fill('/v')
  await expect(page.getByRole('option', { name: '/version 查看版本' })).toBeVisible()
  await composer.press('Tab')
  await expect(composer).toHaveValue('/version ')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await composer.press('Enter')
  await expect(page.getByRole('alert')).toContainText('命令暂不可用')
  await expect(composer).toHaveValue('/version')
  expect(command).toBe('/version')
})
