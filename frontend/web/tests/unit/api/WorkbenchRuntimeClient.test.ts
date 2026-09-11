import { describe, expect, it } from 'vitest'
import {
  consumeEvents,
  safeWebUrl,
  type StreamEvent,
} from '@/api/WorkbenchRuntimeClient'

describe('shared SSE transport', () => {
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
