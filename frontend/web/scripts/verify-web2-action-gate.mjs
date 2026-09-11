import { chromium } from '@playwright/test'
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const baseURL = process.env.NAUMI_E2E_BASE_URL || 'http://localhost:5174/web2'
const sessionId = process.env.NAUMI_E2E_SESSION_ID || '97e3d5b4a2e8'
const workspaceRoot = process.env.NAUMI_E2E_WORKSPACE || 'E:\\Workspace\\NaumiAgent'
const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)
const fileName = process.env.NAUMI_E2E_OUTPUT || `web2_e2e_action_${stamp}.html`
const outputPath = path.join(workspaceRoot, fileName)
const evidenceDir = path.join(workspaceRoot, '.naumi', 'data')
const screenshotPath = path.join(evidenceDir, 'web2-action-gate-e2e.png')
const evidencePath = path.join(evidenceDir, 'web2-action-gate-e2e.json')
const prompt = [
  `创建一个单文件 HTML 页面，文件名必须是 ${fileName}。`,
  '页面标题为“纸上天文台”，使用浅色编辑杂志风格，包含可交互的星图筛选按钮。',
  '必须实际调用文件写入工具写到当前工作目录，完成后检查文件，并在最终答复中给出文件名。',
].join('')

mkdirSync(evidenceDir, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const consoleErrors = []
page.on('console', message => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})
page.on('pageerror', error => consoleErrors.push(String(error)))

let result
try {
  await page.addInitScript(value => {
    localStorage.setItem('naumi:workspace:session', value)
  }, sessionId)
  await page.goto(baseURL, { waitUntil: 'networkidle', timeout: 60_000 })
  await page.getByPlaceholder('描述任务，或输入 / 使用命令').waitFor({ timeout: 30_000 })
  await page.getByLabel('模型').selectOption('openai/kimi-for-coding')

  const processCountBefore = await page.getByLabel('执行过程').count()
  await page.getByPlaceholder('描述任务，或输入 / 使用命令').fill(prompt)
  await page.getByLabel('发送消息').click()

  const deadline = Date.now() + 420_000
  let permissionApprovals = 0
  let runningCaptured = false
  let latestProcessText = ''
  while (Date.now() < deadline) {
    const allowButtons = page.getByRole('button', { name: '允许本次' })
    const allowCount = await allowButtons.count()
    for (let index = 0; index < allowCount; index += 1) {
      const button = allowButtons.nth(index)
      if (await button.isVisible()) {
        await button.click()
        permissionApprovals += 1
      }
    }

    const processes = page.getByLabel('执行过程')
    const currentCount = await processes.count()
    if (currentCount > processCountBefore) {
      latestProcessText = await processes.last().innerText()
      if (!runningCaptured) {
        await page.screenshot({ path: screenshotPath, fullPage: true })
        runningCaptured = true
      }
    }

    const sendReady = await page.getByLabel('发送消息').isVisible().catch(() => false)
    if (sendReady && currentCount > processCountBefore) break
    await page.waitForTimeout(1000)
  }

  const processes = page.getByLabel('执行过程')
  if (await processes.count() <= processCountBefore) {
    throw new Error('页面没有生成新的执行过程组件')
  }
  latestProcessText = await processes.last().innerText()
  const pageText = await page.locator('body').innerText()
  await page.screenshot({ path: screenshotPath, fullPage: true })

  if (!existsSync(outputPath)) throw new Error(`目标文件不存在：${outputPath}`)
  const fileText = readFileSync(outputPath, 'utf8')
  if (statSync(outputPath).size < 500) throw new Error('目标 HTML 文件内容过短')
  if (!/纸上天文台/.test(fileText)) throw new Error('目标 HTML 缺少指定标题')
  if (!/button/i.test(fileText)) throw new Error('目标 HTML 缺少交互按钮')
  if (!/工具调用|写入文件|修改文件/.test(latestProcessText)) {
    throw new Error(`执行过程没有展示文件工具：${latestProcessText}`)
  }
  if (!pageText.includes(fileName)) throw new Error('最终页面没有展示生成文件名')
  if ((latestProcessText.match(/任务已完成/g) || []).length !== 1) {
    throw new Error(`任务终态没有唯一收口：${latestProcessText}`)
  }
  if (/执行进度已更新\s*·\s*已完成/.test(latestProcessText)) {
    throw new Error(`中间阶段仍错误显示任务完成：${latestProcessText}`)
  }

  result = {
    ok: true,
    baseURL,
    sessionId,
    prompt,
    outputPath,
    outputBytes: statSync(outputPath).size,
    permissionApprovals,
    latestProcessText,
    screenshotPath,
    consoleErrors,
    checkedAt: new Date().toISOString(),
  }
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {})
  result = {
    ok: false,
    baseURL,
    sessionId,
    prompt,
    outputPath,
    screenshotPath,
    consoleErrors,
    error: String(error),
    checkedAt: new Date().toISOString(),
  }
} finally {
  writeFileSync(evidencePath, JSON.stringify(result, null, 2), 'utf8')
  await browser.close()
}

console.log(JSON.stringify(result, null, 2))
if (!result.ok) process.exitCode = 1
