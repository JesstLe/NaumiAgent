import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { usePlatform } from '@naumi/shared/platform'
import { useWorkspaceTasks } from './useWorkspaceTasks'
import type {
  ChatSource,
  DaemonStatusResponse,
  GitDiffResponse,
  MessageResponse,
  Session,
  WorkbenchSnapshot,
} from '@naumi/shared/api/types'
import {
  readPreference,
  savePreference,
  WorkbenchRuntimeClient,
  type ModelConfig,
  type Run,
  type StreamEvent,
} from '@naumi/shared/api/WorkbenchRuntimeClient'

export interface Permission {
  callId: string
  name: string
  reason: string
}
export const errorText = (error: unknown) =>
  error instanceof Error ? error.message : '操作未完成，请重试'

export function useWorkspaceController() {
  const platform = usePlatform()
  const [base, setBase] = useState(() =>
    readPreference(
      'api',
      platform.supportsDaemon
        ? 'http://127.0.0.1:8765/api/v1'
        : `${location.origin}/api/v1`,
    ),
  )
  const api = useMemo(
    () => new WorkbenchRuntimeClient(base, () => platform.getToken()),
    [base, platform],
  )
  const [daemon, setDaemon] = useState<DaemonStatusResponse | null>(null)
  const [config, setConfig] = useState<ModelConfig | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<MessageResponse[]>([])
  const [sources, setSources] = useState<ChatSource[]>([])
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null)
  const [diff, setDiff] = useState<GitDiffResponse | null>(null)
  const [error, setError] = useState('')
  const [connecting, setConnecting] = useState(true)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [mutating, setMutating] = useState(false)
  const [draft, setDraftState] = useState(() => readPreference('draft:new'))
  const [model, setModel] = useState('')
  const [mode, setMode] = useState<'default' | 'plan' | 'bypass'>('default')
  const [sendKey, setSendKeyState] = useState(() => readPreference('send-key', 'enter'))
  const setSendKey = (value: string) => {
    setSendKeyState(value)
    savePreference('send-key', value)
  }
  const [createIssue, setCreateIssue] = useState(false)
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [liveEvents, setLiveEvents] = useState<StreamEvent[]>([])
  const controller = useRef<AbortController | null>(null)
  const runId = useRef('')
  const activeId = useRef<string | null>(null)
  const operation = useRef(false)
  const generation = useRef(0)
  const connectionGeneration = useRef(0)
  const stopped = useRef(false)
  const creatingSession = useRef<Promise<Session> | null>(null)

  const setDraft = (value: string) => {
    setDraftState(value)
    savePreference(`draft:${activeId.current ?? 'new'}`, value)
  }
  const refreshSessions = useCallback(async () => {
    const all: Session[] = []
    let page = 1
    while (true) {
      const next = await api.sessions(page++)
      all.push(...next.sessions)
      if (all.length >= next.total || !next.sessions.length) break
    }
    setSessions(all)
    return all
  }, [api])

  const select = useCallback(
    async (id: string | null) => {
      if (operation.current) return
      const current = ++generation.current
      activeId.current = id
      setSessionId(id)
      savePreference('session', id ?? '')
      setMessages([])
      setSources([])
      setSelectedSources([])
      setRuns([])
      setDiff(null)
      setSnapshot(null)
      setPermissions([])
      setLiveEvents([])
      setError('')
      setDraftState(readPreference(`draft:${id ?? 'new'}`))
      if (!id) {
        setLoading(false)
        return
      }
      setLoading(true)
      const results = await Promise.allSettled([
        api.fetchMessages(id, 1, 200),
        api.fetchChatEnvironment(id),
        api.runs(id),
        api.fetchSnapshot(id),
      ])
      if (current !== generation.current) return
      const [history, environment, records, state] = results
      if (history.status === 'fulfilled') {
        let allMessages = history.value.messages
        const total = history.value.total
        try {
          for (let page = 2; allMessages.length < total; page++) {
            const next = await api.fetchMessages(id, page, 200)
            if (current !== generation.current) return
            if (!next.messages.length) break
            allMessages = [...allMessages, ...next.messages]
          }
          setMessages(allMessages)
        } catch (e) {
          if (current === generation.current) setError(errorText(e))
        }
      }
      if (current !== generation.current) return
      if (environment.status === 'fulfilled')
        setSources(environment.value.sources)
      if (records.status === 'fulfilled') setRuns(records.value.runs)
      if (state.status === 'fulfilled') setSnapshot(state.value)
      const failure = results.find((item) => item.status === 'rejected')
      if (failure?.status === 'rejected')
        setError(`部分会话内容未加载：${errorText(failure.reason)}`)
      setLoading(false)
    },
    [api],
  )

  const connect = useCallback(async () => {
    const current = ++connectionGeneration.current
    setConnecting(true)
    setError('')
    try {
      const status = await api.fetchDaemonStatus()
      if (current !== connectionGeneration.current) return
      setDaemon(status)
      await refreshSessions()
      if (current !== connectionGeneration.current) return
      const selected = readPreference('session')
      if (selected) await select(selected)
      // Model discovery can be slower than local session loading.
      api
        .config()
        .then((value) => {
          if (current !== connectionGeneration.current) return
          setConfig(value)
          setModel(
            (previous) =>
              previous ||
              value.models.find((item) => item.tier.includes('capable'))?.id ||
              value.models[0]?.id ||
              '',
          )
        })
        .catch((e) => {
          if (current === connectionGeneration.current)
            setError(`模型列表未加载：${errorText(e)}`)
        })
    } catch (e) {
      if (current === connectionGeneration.current) {
        setDaemon(null)
        setError(`无法连接本地服务：${errorText(e)}`)
      }
    } finally {
      if (current === connectionGeneration.current) setConnecting(false)
    }
  }, [api, refreshSessions, select])

  useEffect(() => {
    void connect()
    return () => {
      connectionGeneration.current++
      generation.current++
    }
  }, [connect])
  useEffect(
    () => () => {
      controller.current?.abort()
    },
    [],
  )
  useEffect(() => {
    if (!daemon || !sessionId) return
    let disposed = false
    let socket: WebSocket | null = null
    let reconnect: ReturnType<typeof setTimeout> | undefined
    const id = sessionId
    const open = async () => {
      if (!daemon.event_stream_url_template) return
      try {
        const url = new URL(
          daemon.event_stream_url_template.replace(
            '{session_id}',
            encodeURIComponent(id),
          ),
        )
        const auth = await platform.getToken()
        if (disposed) return
        if (auth) url.searchParams.set('api_key', auth)
        socket = new WebSocket(url)
        socket.onopen = () => {
          socket?.send(JSON.stringify({ type: 'refresh', limit: 50 }))
          setError((previous) =>
            previous === '实时连接已中断，正在重新连接' ? '' : previous,
          )
        }
        socket.onmessage = (event) => {
          if (disposed || activeId.current !== id) return
          try {
            const envelope = JSON.parse(event.data)
            if (
              envelope.type === 'workbench/snapshot' &&
              envelope.payload?.session_id === id
            )
              setSnapshot(envelope.payload)
          } catch {
            setError('实时状态解析失败，请重新连接')
          }
        }
        socket.onclose = () => {
          if (!disposed) {
            setError((previous) => previous || '实时连接已中断，正在重新连接')
            reconnect = setTimeout(() => void open(), 5000)
          }
        }
      } catch {
        if (!disposed) setError('实时状态连接不可用，请重新连接')
      }
    }
    void open()
    const refresh = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: 'refresh', limit: 50 }))
    }, 10000)
    return () => {
      disposed = true
      clearInterval(refresh)
      clearTimeout(reconnect)
      if (socket) {
        socket.onclose = null
        socket.close()
      }
    }
  }, [daemon, sessionId, platform])
  useEffect(() => {
    const session = sessions.find((item) => item.id === sessionId)
    if (session?.model) setModel(session.model)
  }, [sessionId, sessions])
  useEffect(() => {
    if (!busy) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [busy])

  const ensureSession = async () => {
    if (activeId.current) return activeId.current
    const current = generation.current
    if (!creatingSession.current)
      creatingSession.current = api.create(undefined, model || undefined)
    let session: Session
    try {
      session = await creatingSession.current
    } finally {
      creatingSession.current = null
    }
    if (current !== generation.current)
      throw new Error('会话已切换，请在当前会话重试')
    activeId.current = session.id
    setSessionId(session.id)
    setSessions((previous) => [
      session,
      ...previous.filter((item) => item.id !== session.id),
    ])
    savePreference('session', session.id)
    savePreference(`draft:${session.id}`, draft)
    savePreference('draft:new', '')
    return session.id
  }
  const send = async () => {
    if (!draft.trim() || operation.current || !daemon || loading) return
    operation.current = true
    stopped.current = false
    setBusy(true)
    setError('')
    const content = draft.trim()
    let completed = false
    let failure = ''
    try {
      const id = await ensureSession()
      if (stopped.current) return
      controller.current = new AbortController()
      runId.current = ''
      const messageId = `stream-${crypto.randomUUID()}`
      setMessages((previous) => [
        ...previous,
        {
          id: `user-${messageId}`,
          role: 'user',
          content,
          timestamp: new Date().toISOString(),
          metadata: {},
        },
      ])
      setDraft('')
      setLiveEvents([])
      await api.stream(
        id,
        {
          content,
          runtime_mode: mode,
          source_ids: selectedSources,
          workbench_issue:
            createIssue && snapshot?.missions[0]
              ? {
                  mission_id: snapshot.missions[0].id,
                  title: content.slice(0, 80),
                  description: content,
                }
              : undefined,
        },
        (event) => {
          if (event.run_id || event.data.run_id)
            runId.current = String(event.run_id || event.data.run_id)
          if (event.type === 'token_delta') {
            const token = String(event.data.token ?? event.data.content ?? '')
            setMessages((previous) => {
              const exists = previous.some((item) => item.id === messageId)
              return exists
                ? previous.map((item) =>
                    item.id === messageId
                      ? { ...item, content: item.content + token }
                      : item,
                  )
                : [
                    ...previous,
                    {
                      id: messageId,
                      role: 'assistant',
                      content: token,
                      timestamp: new Date().toISOString(),
                      metadata: {},
                    },
                  ]
            })
          }
          if (
            ['tool_call_start', 'tool_call_end', 'tool_call_error'].includes(
              event.type,
            )
          ) {
            setLiveEvents((previous) => [...previous.slice(-199), event])
          }
          if (event.type === 'permission_request') {
            const callId = String(event.data.call_id ?? '')
            if (callId)
              setPermissions((previous) => [
                ...previous.filter((item) => item.callId !== callId),
                ...(event.data.status === 'needs_confirmation'
                  ? [
                      {
                        callId,
                        name: String(event.data.tool_name ?? '工具执行'),
                        reason: String(event.data.reason ?? ''),
                      },
                    ]
                  : []),
              ])
          }
          if (event.type === 'agent_error')
            failure = String(event.data.message ?? '本次执行失败')
          if (event.type === 'agent_end' && typeof event.data.status === 'string') {
            completed = true
            if (['failed', 'error'].includes(String(event.data.status)))
              failure ||= '本次执行失败，请查看执行记录'
          }
        },
        controller.current.signal,
      )
      if (failure) throw new Error(failure)
      if (!completed && !stopped.current)
        throw new Error('响应连接已中断，请检查执行记录后重试')
      setSelectedSources([])
    } catch (e) {
      if (!stopped.current) {
        setError(errorText(e))
        setDraft(content)
      }
    } finally {
      controller.current = null
      setPermissions([])
      const id = activeId.current
      if (id) {
        const results = await Promise.allSettled([
          api.runs(id),
          refreshSessions(),
        ])
        if (results[0].status === 'fulfilled') setRuns(results[0].value.runs)
        for (const result of results)
          if (result.status === 'rejected')
            setError(
              (previous) =>
                previous || `执行记录更新失败：${errorText(result.reason)}`,
            )
      }
      setBusy(false)
      operation.current = false
    }
  }
  const stop = async () => {
    stopped.current = true
    try {
      if (activeId.current && runId.current)
        await api.cancel(activeId.current, runId.current)
      controller.current?.abort()
    } catch (e) {
      stopped.current = false
      setError(`停止失败：${errorText(e)}`)
    }
  }
  const upload = async (files: FileList | File[]) => {
    if (operation.current || !daemon) return
    // FileList is live: the input is reset immediately after its change event.
    const pendingFiles = Array.from(files)
    if (!pendingFiles.length) return
    operation.current = true
    setUploading(true)
    setError('')
    try {
      const id = await ensureSession()
      for (const file of pendingFiles) {
        const source = await api.uploadChatSource(id, file)
        setSources((previous) => [
          ...previous.filter((item) => item.id !== source.id),
          source,
        ])
        setSelectedSources((previous) => [...previous, source.id])
      }
    } catch (e) {
      setError(`附件上传失败：${errorText(e)}`)
    } finally {
      operation.current = false
      setUploading(false)
    }
  }
  const loadDiff = async () => {
    if (!daemon) {
      setError('请先连接本地服务')
      return
    }
    if (!activeId.current && operation.current) return
    const current = generation.current
    try {
      const id = await ensureSession()
      const next = await api.fetchGitDiff(id)
      if (current === generation.current && activeId.current === id)
        setDiff(next)
    } catch (e) {
      if (current === generation.current) setError(errorText(e))
    }
  }
  const changeModel = async (value: string) => {
    if (operation.current) return
    operation.current = true
    setMutating(true)
    try {
      if (sessionId) {
        const updated = await api.updateSession(sessionId, { model: value })
        setSessions((previous) =>
          previous.map((item) => (item.id === sessionId ? updated : item)),
        )
      }
      setModel(value)
    } catch (e) {
      setError(errorText(e))
    } finally {
      operation.current = false
      setMutating(false)
    }
  }
  const deleteSession = async (id: string) => {
    if (operation.current) return
    operation.current = true
    setMutating(true)
    try {
      const result = (await api.deleteSession(id)) as
        { status?: string } | undefined
      if (result?.status === 'retry_scheduled')
        throw new Error('会话正在清理中，请稍后刷新')
      setSessions((previous) => previous.filter((item) => item.id !== id))
      operation.current = false
      if (id === activeId.current) await select(null)
    } catch (e) {
      setError(errorText(e))
    } finally {
      operation.current = false
      setMutating(false)
    }
  }
  const resolve = async (
    permission: Permission,
    decision: 'allow' | 'deny',
  ) => {
    if (!sessionId) return
    try {
      await api.resolvePermission(sessionId, permission.callId, { decision })
      setPermissions((previous) =>
        previous.filter((item) => item.callId !== permission.callId),
      )
    } catch (e) {
      setError(`审批未成功：${errorText(e)}`)
    }
  }
  const configure = async (url: string, token: string) => {
    try {
      if (operation.current) throw new Error('请等待当前操作结束后再修改连接')
      const parsed = new URL(url, location.origin)
      if (
        !['http:', 'https:'].includes(parsed.protocol) ||
        parsed.username ||
        parsed.password
      )
        throw new Error('请输入有效的 API 地址')
      const next = parsed.href.replace(/\/$/, '')
      await new WorkbenchRuntimeClient(next, async () => token || null).fetchDaemonStatus()
      await platform.setToken(token)
      savePreference('api', next)
      if (next === base) await connect()
      else {
        setDaemon(null)
        setConfig(null)
        setSessions([])
        await select(null)
        setBase(next)
      }
    } catch (e) {
      setError(errorText(e))
      throw e
    }
  }
  const taskState = useWorkspaceTasks(api, sessionId, !!daemon, ensureSession)
  return {
    ...taskState,
    api,
    base,
    daemon,
    config,
    sessions,
    sessionId,
    messages,
    sources,
    selectedSources,
    setSelectedSources,
    runs,
    snapshot,
    diff,
    error,
    setError,
    connecting,
    loading,
    busy,
    uploading,
    draft,
    setDraft,
    model,
    mode,
    sendKey,
    setSendKey,
    setMode,
    permissions,
    liveEvents,
    select,
    connect,
    send,
    stop,
    upload,
    loadDiff,
    changeModel,
    resolve,
    configure,
    createIssue,
    setCreateIssue,
    mutating,
    deleteSession,
  }
}
