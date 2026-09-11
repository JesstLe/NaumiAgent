import { describe, expect, it } from 'vitest'
import { messageSources } from '../../../web2/src/community/AiSources'

describe('messageSources', () => {
  it('prefers real structured citations and rejects unsafe URLs', () => {
    const sources = messageSources({
      id: 'm1',
      role: 'assistant',
      content: 'See https://fallback.example/page',
      timestamp: '2026-09-11T10:00:00+08:00',
      metadata: {
        citations: [
          { id: 'safe', title: 'Primary source', url: 'https://docs.example/path', snippet: 'Verified detail' },
          { id: 'unsafe', title: 'Unsafe', url: 'javascript:alert(1)' },
        ],
      },
    })
    expect(sources).toEqual([{
      id: 'safe',
      title: 'Primary source',
      url: 'https://docs.example/path',
      snippet: 'Verified detail',
    }])
  })

  it('deduplicates links already present in assistant content', () => {
    const sources = messageSources({
      id: 'm2',
      role: 'assistant',
      content: '[Reference](https://example.com/a) and https://example.com/a.',
      timestamp: '2026-09-11T10:00:00+08:00',
      metadata: {},
    })
    expect(sources).toHaveLength(1)
    expect(sources[0].title).toBe('Reference')
    expect(sources[0].url).toBe('https://example.com/a')
  })
})
