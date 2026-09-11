import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchApiClient } from '../../src/api/WorkbenchApiClient'

const base = 'http://localhost:9889/api/v1'
const server = setupServer()
const client = new WorkbenchApiClient(base, async () => null)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('archives using the real session route and accepts an empty 204 response', async () => {
  let calls = 0
  server.use(http.post(`${base}/sessions/one/archive`, () => {
    calls++
    return new HttpResponse(null, { status: 204 })
  }))
  await expect(client.archiveSession('one')).resolves.toBeUndefined()
  expect(calls).toBe(1)
})

it.each([
  [404, 'Not Found', '当前后端未提供归档接口，请更新并重启后端服务后重试'],
  [404, 'Session not found', '会话不存在，请刷新会话列表后重试'],
  [409, '当前会话正在执行，完成或停止后才能归档', '当前会话正在执行，完成或停止后才能归档'],
])('reports archive failure %s (%s) without retrying', async (status, detail, message) => {
  let calls = 0
  server.use(http.post(`${base}/sessions/one/archive`, () => {
    calls++
    return HttpResponse.json({ detail }, { status })
  }))
  await expect(client.archiveSession('one')).rejects.toMatchObject({ status, message })
  expect(calls).toBe(1)
})
