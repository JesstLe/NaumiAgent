import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkspaceProvider, useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { PlatformProvider } from '@naumi/shared/platform'
import { savePreference } from '@naumi/shared/api/WorkbenchRuntimeClient'

const base = 'http://localhost:9888/api/v1'
const session = (id: string) => ({
  id,
  title: id,
  model: 'test-model',
  message_count: 0,
  updated_at: '',
  created_at: '',
  status: 'active',
  total_tokens: 0,
  total_cost_usd: 0,
})
const snapshot = {
  version: 1,
  session_id: 'one',
  summary: {},
  missions: [],
  tasks: [],
  issues: [],
  worktrees: [],
  events: [],
  agent_profiles: [],
  approvals: [],
}
let posts = 0
let sentBody: Record<string, unknown> = {}
const server = setupServer(
  http.get(`${base}/workbench/daemon/status`, () =>
    HttpResponse.json({
      workspace_name: '真实接口契约测试',
      event_stream_url_template: '',
    }),
  ),
  http.get(`${base}/sessions`, () =>
    HttpResponse.json({ sessions: [session('one'), session('two')], total: 2 }),
  ),
  http.get(`${base}/config`, () =>
    HttpResponse.json({
      models: [{ id: 'test-model', name: '测试模型', tier: 'capable' }],
      tools: [],
    }),
  ),
  http.get(`${base}/sessions/:id/messages`, ({ params }) =>
    HttpResponse.json({
      messages: [
        {
          id: 'history',
          role: 'assistant',
          content: `历史-${params.id}`,
          timestamp: '',
          metadata: {},
        },
      ],
      total: 1,
    }),
  ),
  http.get(`${base}/sessions/:id/environment`, () =>
    HttpResponse.json({ sources: [] }),
  ),
  http.get(`${base}/sessions/:id/runs`, () => HttpResponse.json({ runs: [] })),
  http.get(`${base}/workbench/sessions/:id/snapshot`, () =>
    HttpResponse.json(snapshot),
  ),
  http.post(`${base}/sessions`, () => HttpResponse.json(session('created'))),
  http.post(`${base}/sessions/:id/messages`, async ({ request }) => {
    posts++
    sentBody = (await request.json()) as Record<string, unknown>
    return new HttpResponse(
      'data: {"id":"t","type":"token_delta","data":{"token":"共享答复"}}\n\ndata: {"id":"e","type":"agent_end","data":{"status":"completed"}}\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } },
    )
  }),
  http.patch(`${base}/sessions/:id`, async ({ request, params }) =>
    HttpResponse.json({
      ...session(String(params.id)),
      ...((await request.json()) as object),
    }),
  ),
)

function View({ label }: { label: string }) {
  const w = useWorkspace()
  return (
    <section aria-label={label}>
      <div data-testid={`${label}-connected`}>
        {w.daemon ? 'connected' : 'offline'}
      </div>
      <div data-testid={`${label}-session`}>{w.sessionId}</div>
      <div data-testid={`${label}-messages`}>
        {w.messages.map((message) => message.content).join('|')}
      </div>
      <div data-testid={`${label}-error`}>{w.error}</div>
      <div data-testid={`${label}-failed-message`}>{w.failedMessage}</div>
      <input
        aria-label={`${label}-draft`}
        value={w.draft}
        onChange={(e) => w.setDraft(e.target.value)}
      />
      <button onClick={() => void w.select('one')}>选择 {label} one</button>
      <button onClick={() => void w.select('two')}>选择 {label} two</button>
      <button
        onClick={() => {
          void w.send()
          void w.send()
        }}
      >
        发送 {label}
      </button>
      <button onClick={() => void w.changeModel('other-model')}>
        模型 {label}
      </button>
      <button onClick={() => void w.stop()}>停止 {label}</button>
      <button
        disabled={!w.failedMessage || w.busy}
        onClick={() => void w.retryFailedSend()}
      >
        重试 {label}
      </button>
      <button
        onClick={() => {
          const files = [new File(['真实附件内容'], 'note.txt', { type: 'text/plain' })]
          void w.upload(files)
          files.length = 0
        }}
      >
        附件 {label}
      </button>
      {w.permissions.map((p) => (
        <button key={p.callId} onClick={() => void w.resolve(p, 'allow')}>
          允许 {label} {p.name}
        </button>
      ))}
      <div data-testid={`${label}-busy`}>{String(w.busy)}</div>
      <div data-testid={`${label}-sources`}>{w.selectedSources.join(',')}</div>
      <div data-testid={`${label}-model`}>{w.model}</div>
    </section>
  )
}
function setup() {
  render(
    <PlatformProvider>
      <WorkspaceProvider>
        <View label="web" />
        <View label="web2" />
      </WorkspaceProvider>
    </PlatformProvider>,
  )
}
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => {
  localStorage.clear()
  savePreference('api', base)
  posts = 0
})
afterEach(() => {
  cleanup()
  server.resetHandlers()
  vi.restoreAllMocks()
})
afterAll(() => server.close())

describe('one shared workspace for two presentation shells', () => {
  it('shares drafts, session selection, model updates and one stream; blocks duplicate sends', async () => {
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.click(screen.getByText('选择 web one'))
    await waitFor(() =>
      expect(screen.getByTestId('web2-messages')).toHaveTextContent('历史-one'),
    )
    fireEvent.change(screen.getByLabelText('web-draft'), {
      target: { value: '共享草稿' },
    })
    expect(screen.getByLabelText('web2-draft')).toHaveValue('共享草稿')
    fireEvent.click(screen.getByText('模型 web2'))
    await waitFor(() =>
      expect(screen.getByTestId('web-model')).toHaveTextContent('other-model'),
    )
    fireEvent.click(screen.getByText('发送 web2'))
    await waitFor(() =>
      expect(screen.getByTestId('web-messages')).toHaveTextContent('共享答复'),
    )
    expect(posts).toBe(1)
    expect(sentBody.stream).toBe(true)
    expect(screen.getByLabelText('web-draft')).toHaveValue('')
  })
  it('ignores stale history responses after rapid switching and restores each draft', async () => {
    let release: (() => void) | undefined
    server.use(
      http.get(`${base}/sessions/one/messages`, async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return HttpResponse.json({
          messages: [
            {
              id: 'old',
              role: 'assistant',
              content: 'stale-one',
              metadata: {},
            },
          ],
          total: 1,
        })
      }),
    )
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.click(screen.getByText('选择 web one'))
    await waitFor(() => expect(release).toBeDefined())
    fireEvent.change(screen.getByLabelText('web-draft'), {
      target: { value: 'one 草稿' },
    })
    fireEvent.click(screen.getByText('选择 web2 two'))
    await waitFor(() =>
      expect(screen.getByTestId('web-messages')).toHaveTextContent('历史-two'),
    )
    await act(async () => {
      release?.()
    })
    expect(screen.getByTestId('web-messages')).not.toHaveTextContent(
      'stale-one',
    )
    expect(localStorage.getItem('naumi:workspace:draft:one')).toBe('one 草稿')
  })
  it('keeps the original draft and retries the failed send for both views', async () => {
    let attempts = 0
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.post(`${base}/sessions/:id/messages`, async ({ request }) => {
        attempts++
        bodies.push((await request.json()) as Record<string, unknown>)
        if (attempts === 1)
          return HttpResponse.json({ detail: '服务暂不可用' }, { status: 503 })
        return new HttpResponse(
          'data: {"id":"t","type":"token_delta","data":{"token":"重试成功"}}\n\ndata: {"id":"e","type":"agent_end","data":{"status":"completed"}}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }),
    )
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.change(screen.getByLabelText('web2-draft'), {
      target: { value: '不要丢失这条任务' },
    })
    fireEvent.click(screen.getByText('发送 web2'))
    await waitFor(() =>
      expect(screen.getByTestId('web-error')).toHaveTextContent('服务暂不可用'),
    )
    expect(screen.getByLabelText('web-draft')).toHaveValue('不要丢失这条任务')
    expect(screen.getByTestId('web-failed-message')).toHaveTextContent('不要丢失这条任务')
    expect(screen.getByTestId('web2-failed-message')).toHaveTextContent('不要丢失这条任务')

    fireEvent.click(screen.getByText('重试 web'))
    await waitFor(() =>
      expect(screen.getByTestId('web2-messages')).toHaveTextContent('重试成功'),
    )
    expect(attempts).toBe(2)
    expect(bodies[0].content).toBe('不要丢失这条任务')
    expect(bodies[1].content).toBe('不要丢失这条任务')
    expect(screen.getByTestId('web-error')).toBeEmptyDOMElement()
    expect(screen.getByTestId('web2-failed-message')).toBeEmptyDOMElement()
    expect(
      screen
        .getByTestId('web-messages')
        .textContent?.match(/不要丢失这条任务/g),
    ).toHaveLength(1)
  })
  it('does not mistake a response boundary for a completed server run', async () => {
    server.use(http.post(`${base}/sessions/:id/messages`, () => new HttpResponse('data: {"id":"boundary","type":"agent_end","data":{}}\n\n', { headers: { 'Content-Type': 'text/event-stream' } })))
    setup()
    await waitFor(() => expect(screen.getByTestId('web-connected')).toHaveTextContent('connected'))
    fireEvent.change(screen.getByLabelText('web-draft'), { target: { value: '断流也必须保留' } })
    fireEvent.click(screen.getByText('发送 web'))
    await waitFor(() => expect(screen.getByTestId('web2-error')).toHaveTextContent('响应连接已中断'))
    expect(screen.getByLabelText('web2-draft')).toHaveValue('断流也必须保留')
  })
  it('shares attachment selection and submits only selected source IDs', async () => {
    server.use(
      http.post(`${base}/sessions/:id/upload`, () =>
        HttpResponse.json({
          id: 'source-1',
          title: 'note.txt',
          path: 'note.txt',
          kind: 'file',
        }),
      ),
    )
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.click(screen.getByText('附件 web'))
    await waitFor(() =>
      expect(screen.getByTestId('web2-sources')).toHaveTextContent('source-1'),
    )
    fireEvent.change(screen.getByLabelText('web2-draft'), {
      target: { value: '读取附件' },
    })
    fireEvent.click(screen.getByText('发送 web2'))
    await waitFor(() =>
      expect(screen.getByTestId('web-messages')).toHaveTextContent('共享答复'),
    )
    expect(sentBody.source_ids).toEqual(['source-1'])
  })
  it('resolves streamed permissions from either shell before continuing', async () => {
    let output: ReadableStreamDefaultController<Uint8Array>
    let resolved: unknown
    server.use(
      http.post(
        `${base}/sessions/:id/messages`,
        () =>
          new HttpResponse(
            new ReadableStream({
              start(c) {
                output = c
                c.enqueue(
                  new TextEncoder().encode(
                    'data: {"id":"permission","type":"permission_request","run_id":"run-1","data":{"call_id":"call-1","tool_name":"file_write","reason":"需要写入文件","status":"needs_confirmation"}}\n\n',
                  ),
                )
              },
            }),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
      ),
      http.post(
        `${base}/sessions/:id/permissions/call-1/resolve`,
        async ({ request }) => {
          resolved = await request.json()
          output.enqueue(
            new TextEncoder().encode(
              'data: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n',
            ),
          )
          output.close()
          return HttpResponse.json({ status: 'resolved' })
        },
      ),
    )
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.change(screen.getByLabelText('web-draft'), {
      target: { value: '需要审批的任务' },
    })
    fireEvent.click(screen.getByText('发送 web'))
    await screen.findByText('允许 web2 file_write')
    fireEvent.click(screen.getByText('允许 web2 file_write'))
    await waitFor(() =>
      expect(screen.getByTestId('web-busy')).toHaveTextContent('false'),
    )
    expect(resolved).toEqual({ decision: 'allow' })
  })
  it('allows navigation while a run continues and keeps streamed output with its session', async () => {
    let output: ReadableStreamDefaultController<Uint8Array>
    server.use(
      http.post(
        `${base}/sessions/one/messages`,
        () => new HttpResponse(new ReadableStream({
          start(controller) {
            output = controller
            controller.enqueue(new TextEncoder().encode(
              'data: {"id":"thinking","type":"thinking_start","data":{}}\n\n',
            ))
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } }),
      ),
    )
    setup()
    await waitFor(() => expect(screen.getByTestId('web-connected')).toHaveTextContent('connected'))
    fireEvent.click(screen.getByText('选择 web one'))
    await waitFor(() => expect(screen.getByTestId('web-messages')).toHaveTextContent('历史-one'))
    fireEvent.change(screen.getByLabelText('web-draft'), { target: { value: '后台继续执行' } })
    fireEvent.click(screen.getByText('发送 web'))
    await waitFor(() => expect(screen.getByTestId('web-busy')).toHaveTextContent('true'))

    fireEvent.click(screen.getByText('选择 web2 two'))
    await waitFor(() => expect(screen.getByTestId('web-messages')).toHaveTextContent('历史-two'))
    expect(screen.getByLabelText('web2-draft')).toBeEnabled()

    output.enqueue(new TextEncoder().encode(
      'data: {"id":"answer","type":"token_delta","data":{"token":"原会话答复"}}\n\ndata: {"id":"end","type":"agent_end","data":{"status":"completed"}}\n\n',
    ))
    output.close()
    await waitFor(() => expect(screen.getByTestId('web-busy')).toHaveTextContent('false'))
    expect(screen.getByTestId('web-messages')).toHaveTextContent('历史-two')
    expect(screen.getByTestId('web-messages')).not.toHaveTextContent('原会话答复')
  })
  it('stops the server run from the other shell and clears the busy state', async () => {
    let output: ReadableStreamDefaultController<Uint8Array>
    let cancelled = false
    server.use(
      http.post(
        `${base}/sessions/:id/messages`,
        () =>
          new HttpResponse(
            new ReadableStream({
              start(c) {
                output = c
                c.enqueue(
                  new TextEncoder().encode(
                    'data: {"id":"token","type":"token_delta","run_id":"run-2","data":{"token":"执行中"}}\n\n',
                  ),
                )
              },
            }),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
      ),
      http.post(`${base}/sessions/:id/runs/run-2/cancel`, () => {
        cancelled = true
        output.close()
        return HttpResponse.json({ status: 'cancellation_requested' })
      }),
    )
    setup()
    await waitFor(() =>
      expect(screen.getByTestId('web-connected')).toHaveTextContent(
        'connected',
      ),
    )
    fireEvent.change(screen.getByLabelText('web2-draft'), {
      target: { value: '可停止任务' },
    })
    fireEvent.click(screen.getByText('发送 web2'))
    await waitFor(() =>
      expect(screen.getByTestId('web-messages')).toHaveTextContent('执行中'),
    )
    fireEvent.click(screen.getByText('停止 web'))
    await waitFor(() =>
      expect(screen.getByTestId('web2-busy')).toHaveTextContent('false'),
    )
    expect(cancelled).toBe(true)
    expect(screen.getByTestId('web2-error')).toBeEmptyDOMElement()
  })
})
