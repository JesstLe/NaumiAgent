import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkbenchRequestError, WorkbenchRuntimeClient, type PersonalTeamTurnInput } from '../../src/api/WorkbenchRuntimeClient'

afterEach(() => vi.unstubAllGlobals())
const client = () => new WorkbenchRuntimeClient('http://localhost:9888/api/v1/', async () => 'local-test-token')
const body: PersonalTeamTurnInput = { team_id: `pa_${'a'.repeat(32)}`, team_version: 3, source_session_id: 'team-daily',
  request_id: 'stable-team-request', content: '核对预算并给出结果。', context: '只读取指定文件', source_ids: ['source-one'],
  member_grants: [{ agent_id: `pa_${'b'.repeat(32)}`, agent_version: 2, tool_names: ['file_read'] }] }

describe('persistent personal team turn client', () => {
  it('sends the exact original identity, team version, sources and selected grants', async () => {
    const result = { id: `pt_${'c'.repeat(32)}`, request_id: body.request_id }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(result)))
    vi.stubGlobal('fetch', fetcher)
    expect(await client().createPersonalTeamTurn(body)).toEqual(result)
    const [url, options] = fetcher.mock.calls[0]
    expect(url).toBe('http://localhost:9888/api/v1/personal/team-turns')
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual(body)
    expect(options.headers.Authorization).toBe('Bearer local-test-token')
  })
  it('lists only the exact team session and encodes path parameters', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{"turns":[]}'))
    vi.stubGlobal('fetch', fetcher)
    await client().personalTeamTurns('daily / team')
    await client().personalTeamTurn('task/id')
    expect(fetcher.mock.calls[0][0]).toBe('http://localhost:9888/api/v1/personal/team-turns?team_session_id=daily%20%2F%20team')
    expect(fetcher.mock.calls[1][0]).toBe('http://localhost:9888/api/v1/personal/team-turns/task%2Fid')
  })
  it('cancels the exact accepted turn without submitting a new task', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"status":"cancelled"}'))
    vi.stubGlobal('fetch', fetcher)
    await client().cancelPersonalTeamTurn('pt_accepted')
    expect(fetcher.mock.calls[0][0]).toBe('http://localhost:9888/api/v1/personal/team-turns/pt_accepted/cancel')
    expect(fetcher.mock.calls[0][1].method).toBe('POST')
    expect(fetcher.mock.calls[0][1].body).toBe('{}')
  })
  it('preserves storage and version error codes without inventing acceptance state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"code":"version_conflict","detail":"团队版本已更新"}', { status: 409 })))
    const error = await client().createPersonalTeamTurn(body).catch(value => value)
    expect(error).toBeInstanceOf(WorkbenchRequestError)
    expect(error).toMatchObject({ status: 409, code: 'version_conflict', message: '团队版本已更新' })
    expect(error).not.toHaveProperty('accepted')
  })
})
