import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import { WorkbenchRuntimeClient, type PersonalMessageInput } from '@naumi/shared/api/WorkbenchRuntimeClient'

afterEach(() => vi.restoreAllMocks())
const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
const client = () => new WorkbenchRuntimeClient('http://localhost/api/v1/', async () => 'peer-contract-key')

describe('persistent specialist message HTTP contract', () => {
  it('encodes collaboration and message identity and sends authenticated requests', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => response({ messages: [] }))
    await client().personalMessages('pc/with space')
    await client().personalMessage('pm/with space')
    expect(fetch.mock.calls[0][0]).toBe('http://localhost/api/v1/personal/collaborations/pc%2Fwith%20space/messages')
    expect(fetch.mock.calls[1][0]).toBe('http://localhost/api/v1/personal/messages/pm%2Fwith%20space')
    expect(fetch.mock.calls[0][1]?.headers).toMatchObject({ Authorization: 'Bearer peer-contract-key' })
  })

  it('posts an exact manual request, frozen source/recipient versions and explicit tool scopes', async () => {
    const body: PersonalMessageInput = { source_session_id: 'main', request_id: 'stable-message',
      sender_agent_id: `pa_${'a'.repeat(32)}`, sender_agent_version: 3,
      recipient_agent_id: `pa_${'b'.repeat(32)}`, recipient_agent_version: 4,
      content: '请读取指定资料核对预算。', context: '只共享这段上下文',
      peer_agents: [{ agent_id: `pa_${'b'.repeat(32)}`, agent_version: 4, tool_names: ['file_read'] }] }
    const rootRequestId = `message-${createHash('sha256').update(body.request_id).digest('hex')}`
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ id: 'pc_received', request_id: rootRequestId, message_request_id: body.request_id }))
    const result = await client().createPersonalMessage(body)
    expect(fetch).toHaveBeenCalledWith('http://localhost/api/v1/personal/messages', expect.objectContaining({ method: 'POST', body: JSON.stringify(body) }))
    expect(result.request_id).toBe(rootRequestId)
    expect(result.message_request_id).toBe(body.request_id)
  })

  it('acknowledges only the expected response digest rather than broad mailbox state', async () => {
    const body = { expected_response_sha256: 'd'.repeat(64) }
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ id: 'pm_received' }))
    await client().acknowledgePersonalMessage('pm/one', body)
    expect(fetch).toHaveBeenCalledWith('http://localhost/api/v1/personal/messages/pm%2Fone/ack', expect.objectContaining({ method: 'POST', body: JSON.stringify(body) }))
  })

  it('preserves delivery/version errors as explicit failures', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: '消息结果已变更，请重新核对原始回应', code: 'binding_conflict' }), { status: 409 }))
    await expect(client().acknowledgePersonalMessage('pm_existing', { expected_response_sha256: 'e'.repeat(64) })).rejects.toThrow('消息结果已变更')
  })
})
