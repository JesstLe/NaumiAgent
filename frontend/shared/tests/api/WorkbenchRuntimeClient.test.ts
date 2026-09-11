import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  consumeEvents,
  readPreference,
  safeWebUrl,
  savePreference,
  WorkbenchRuntimeClient,
  type StreamEvent,
} from '@naumi/shared/api/WorkbenchRuntimeClient'

beforeEach(() => {
  localStorage.clear()
  window.history.replaceState({}, '', '/')
})

afterEach(() => vi.restoreAllMocks())

describe('shared SSE transport', () => {
  it('keeps the edited message id when rerunning a slash command', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      'data: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n',
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    ))
    const client = new WorkbenchRuntimeClient('http://localhost/api/v1', async () => null)

    await client.stream(
      'session-1',
      { content: '/version', edit_message_id: 'msg-2' },
      () => {},
      new AbortController().signal,
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost/api/v1/sessions/session-1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          command: '/version',
          runtime_mode: 'default',
          edit_message_id: 'msg-2',
        }),
      }),
    )
  })
  it('preserves Chinese UTF-8 across byte chunks and CRLF boundaries', async () => {
    const encoded = new TextEncoder().encode(
      ': heartbeat\r\n\r\ndata: {"id":"1","type":"token_delta","data":{"token":"你好"}}\r\n\r\ndata: {"id":"2","type":"agent_end","data":{}}',
    )
    const events: StreamEvent[] = []
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of encoded) controller.enqueue(new Uint8Array([byte]))
        controller.close()
      },
    })
    await consumeEvents(body, (event) => events.push(event))
    expect(events.map((event) => event.type)).toEqual([
      'token_delta',
      'agent_end',
    ])
    expect(events[0].data.token).toBe('你好')
  })
  it('handles empty streams and reports malformed server events', async () => {
    await expect(
      consumeEvents(
        new ReadableStream({
          start(c) {
            c.close()
          },
        }),
        () => {},
      ),
    ).resolves.toBeUndefined()
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('data: broken\n\n'))
        c.close()
      },
    })
    await expect(consumeEvents(body, () => {})).rejects.toThrow()
  })
})

describe('browser URL validation', () => {
  it('accepts web destinations and rejects executable schemes and embedded credentials', () => {
    expect(safeWebUrl('example.com')).toBe('https://example.com/')
    expect(safeWebUrl('http://localhost:5174/web2')).toBe(
      'http://localhost:5174/web2',
    )
    for (const url of [
      'javascript:alert(1)',
      'file:///C:/secret',
      'https://user:password@example.com',
      'data:text/html,<script>',
    ]) {
      expect(() => safeWebUrl(url)).toThrow()
    }
  })
})

describe('workspace window preferences', () => {
  it('seeds an auxiliary window from launch parameters and keeps later changes', () => {
    window.history.replaceState({}, '', '/?naumiWindow=workspace-1&naumiApi=http%3A%2F%2F127.0.0.1%3A8770%2Fapi%2Fv1&naumiSession=session-one')

    expect(readPreference('api')).toBe('http://127.0.0.1:8770/api/v1')
    expect(readPreference('session')).toBe('session-one')
    savePreference('session', 'session-two')
    expect(readPreference('session')).toBe('session-two')
  })

  it('isolates drafts and API addresses between desktop windows', () => {
    window.history.replaceState({}, '', '/?naumiWindow=workspace-1')
    savePreference('draft:new', '第一个窗口')
    savePreference('api', 'http://127.0.0.1:8770/api/v1')

    window.history.replaceState({}, '', '/?naumiWindow=workspace-2')
    expect(readPreference('draft:new')).toBe('')
    expect(readPreference('api', 'fallback')).toBe('fallback')
    savePreference('draft:new', '第二个窗口')

    window.history.replaceState({}, '', '/?naumiWindow=workspace-1')
    expect(readPreference('draft:new')).toBe('第一个窗口')
    expect(readPreference('api')).toBe('http://127.0.0.1:8770/api/v1')
  })

  it('preserves the existing primary-window storage keys', () => {
    savePreference('api', 'http://127.0.0.1:8765/api/v1')
    expect(localStorage.getItem('naumi:workspace:api')).toBe('http://127.0.0.1:8765/api/v1')
  })
})
