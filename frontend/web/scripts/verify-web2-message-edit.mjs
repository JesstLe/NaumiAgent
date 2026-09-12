import { chromium } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const workspaceRoot = process.env.NAUMI_E2E_WORKSPACE || 'E:\\Workspace\\NaumiAgent'
const webURL = process.env.NAUMI_E2E_BASE_URL || 'http://localhost:5174/web2'
const apiBase = process.env.NAUMI_E2E_API_BASE || 'http://127.0.0.1:8765/api/v1'
const evidenceDir = path.join(workspaceRoot, '.naumi', 'data')
const evidencePath = path.join(evidenceDir, 'web2-message-edit-e2e.json')
const screenshotPath = path.join(evidenceDir, 'web2-message-edit-e2e.png')
const initialPrompt = '只回复：初始版本'
const editedPrompt = '只回复：编辑后的版本'

mkdirSync(evidenceDir, { recursive: true })

async function jsonRequest(url, init = {}) {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  })
  if (!response.ok) throw new Error(`${init.method || 'GET'} ${url} 返回 ${response.status}`)
  return response.json()
}

const session = await jsonRequest(`${apiBase}/sessions`, {
  method: 'POST',
  body: JSON.stringify({
    title: 'Web2 消息编辑端到端验收',
    model: 'openai/kimi-for-coding',
  }),
})

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const consoleErrors = []
const failedResources = []
page.on('console', message => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})
page.on('pageerror', error => consoleErrors.push(String(error)))
page.on('response', response => {
  if (response.status() >= 400) failedResources.push({
    status: response.status(),
    url: response.url(),
  })
})

let result
try {
  await page.addInitScript(sessionId => {
    localStorage.setItem('naumi:workspace:session', sessionId)
  }, session.id)
  await page.goto(webURL, { waitUntil: 'networkidle', timeout: 60_000 })
  const composer = page.getByPlaceholder('描述任务，或输入 / 使用命令')
  await composer.waitFor({ timeout: 30_000 })
  await composer.fill(initialPrompt)
  await page.getByRole('button', { name: '发送消息' }).click()
  await page.getByRole('button', { name: '发送消息' }).waitFor({ state: 'visible', timeout: 180_000 })

  const conversation = page.getByLabel('对话', { exact: true })
  const initialUser = conversation.getByRole('article').filter({ hasText: initialPrompt })
  await initialUser.waitFor({ state: 'visible' })
  const initialActions = page.locator('.community-message-action-bar')
  await initialActions.last().waitFor({ state: 'visible', timeout: 30_000 })
  const initialActionCount = await initialActions.count()

  await initialUser.hover()
  await initialUser.getByRole('button', { name: '编辑消息' }).click()
  const editor = page.getByRole('textbox', { name: '编辑已发送消息' })
  await editor.fill(editedPrompt)
  await page.getByRole('button', { name: '发送修改' }).click()

  const runningActionCount = await page.locator('.community-message-action-bar').count()
  const stopVisible = await page.getByRole('button', { name: '停止执行' }).isVisible().catch(() => false)
  if (runningActionCount !== 0) throw new Error(`编辑运行期间仍显示 ${runningActionCount} 个助手操作区`)

  await page.getByRole('button', { name: '发送消息' }).waitFor({ state: 'visible', timeout: 180_000 })
  const editedUser = conversation.getByRole('article').filter({ hasText: editedPrompt })
  await editedUser.waitFor({ state: 'visible' })
  const editedAssistant = conversation.getByRole('article').filter({ hasText: '编辑后的版本' }).filter({ hasNotText: editedPrompt })
  await editedAssistant.waitFor({ state: 'visible', timeout: 30_000 })
  await page.locator('.community-message-action-bar').last().waitFor({ state: 'visible', timeout: 30_000 })
  const finalActionCount = await page.locator('.community-message-action-bar').count()

  await page.reload({ waitUntil: 'networkidle', timeout: 60_000 })
  const reloadedConversation = page.getByLabel('对话', { exact: true })
  await reloadedConversation.getByText(editedPrompt, { exact: true }).waitFor({ state: 'visible' })
  const oldPromptCount = await reloadedConversation.getByText(initialPrompt, { exact: true }).count()
  const reloadedActionCount = await page.locator('.community-message-action-bar').count()
  await page.screenshot({ path: screenshotPath, fullPage: true })

  const persisted = await jsonRequest(`${apiBase}/sessions/${encodeURIComponent(session.id)}/messages?page=1&page_size=200`)
  const users = persisted.messages.filter(message => message.role === 'user').map(message => message.content)
  const assistants = persisted.messages.filter(message => message.role === 'assistant').map(message => message.content)
  const runs = await jsonRequest(`${apiBase}/sessions/${encodeURIComponent(session.id)}/runs?limit=20`)
  const latestRun = runs.runs[0]

  if (oldPromptCount !== 0) throw new Error('刷新后旧用户消息仍然存在')
  if (users.length !== 1 || users[0] !== editedPrompt) {
    throw new Error(`持久化用户分支不正确：${JSON.stringify(users)}`)
  }
  if (!assistants.at(-1)?.includes('编辑后的版本')) {
    throw new Error(`持久化助手答复不正确：${JSON.stringify(assistants)}`)
  }
  if (!['completed', 'completed_unverified'].includes(latestRun?.status)) {
    throw new Error(`编辑后的 Run 未正常完成：${latestRun?.status || 'missing'}`)
  }
  if (reloadedActionCount !== 1) {
    throw new Error(`刷新后助手操作区数量不正确：${reloadedActionCount}`)
  }

  result = {
    ok: true,
    webURL,
    apiBase,
    sessionId: session.id,
    model: session.model,
    initialPrompt,
    editedPrompt,
    initialActionCount,
    runningActionCount,
    stopVisible,
    finalActionCount,
    reloadedActionCount,
    persistedUserMessages: users,
    persistedAssistantCount: assistants.length,
    latestRunId: latestRun.id,
    latestRunStatus: latestRun.status,
    screenshotPath,
    consoleErrors,
    failedResources,
    checkedAt: new Date().toISOString(),
  }
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {})
  result = {
    ok: false,
    webURL,
    apiBase,
    sessionId: session.id,
    model: session.model,
    screenshotPath,
    consoleErrors,
    failedResources,
    error: String(error),
    checkedAt: new Date().toISOString(),
  }
} finally {
  writeFileSync(evidencePath, JSON.stringify(result, null, 2), 'utf8')
  await browser.close()
}

console.log(JSON.stringify(result, null, 2))
if (!result.ok) process.exitCode = 1
