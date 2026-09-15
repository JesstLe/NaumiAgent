import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { usePlatform } from '@naumi/shared/platform'
import { isTimelineEvent } from '@naumi/shared/api/activity'
import { useWorkspaceTasks } from './useWorkspaceTasks'
import { useWorkspaceGoals } from './useWorkspaceGoals'
import { workspacePathsMatch } from '@naumi/shared/projects/workspaceProjects'
import type {
  ChatSource,
  DaemonStatusResponse,
  EngineInfo,
  GitDiffResponse,
  MessageResponse,
  Session,
  WorkbenchSnapshot,
} from '@naumi/shared/api/types'
import {
  readPreference,
  savePreference,
  buildWorkbenchEventStreamUrl,
  WorkbenchRuntimeClient,
  type ModelConfig,
  type Run,
  type StreamEvent,
  type SlashCommand,
} from '@naumi/shared/api/WorkbenchRuntimeClient'

export interface Permission {
  sessionId: string
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
  const daemonRef = useRef<DaemonStatusResponse | null>(null)
  const [config, setConfig] = useState<ModelConfig | null>(null)
  const [commands, setCommands] = useState<SlashCommand[]>([])
  const [commandsError, setCommandsError] = useState('')
  const [sessions, setSessions] = useState<Session[]>([])
  const sessionsRef = useRef<Session[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<MessageResponse[]>([])
  const [sources, setSources] = useState<ChatSource[]>([])
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null)
  const [snapshotLoading, setSnapshotLoading] = useState(false)
  const [snapshotError, setSnapshotError] = useState('')
  const snapshotRevision = useRef(0)
  const [diff, setDiff] = useState<GitDiffResponse | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState('')
  const [diffUpdatedAt, setDiffUpdatedAt] = useState('')
  const diffRevision = useRef(0)
  const [error, setError] = useState('')
  const [failedSend, setFailedSend] = useState<{
    content: string
    error: string
    userMessageId?: string
    assistantMessageId?: string
  } | null>(null)
  const [connecting, setConnecting] = useState(true)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [runningUserMessageId, setRunningUserMessageId] = useState<string | null>(null)
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
  const [engine, setEngineState] = useState<'naumi' | 'pi'>('naumi')
  const [engines, setEngines] = useState<EngineInfo[]>([
    { id: 'naumi', name: 'NaumiAgent 引擎', available: true, default: true },
  ])
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [liveEvents, setLiveEvents] = useState<StreamEvent[]>([])
  const controller = useRef<AbortController | null>(null)
  const runId = useRef('')
  const runningSessionId = useRef<string | null>(null)
  const [visibleRunningSessionId, setVisibleRunningSessionId] = useState<string | null>(null)
  const activeId = useRef<string | null>(null)
  const operation = useRef(false)
  const generation = useRef(0)
  const connectionGeneration = useRef(0)
  const stopped = useRef(false)
  const creatingSession = useRef<Promise<Session> | null>(null)

  useEffect(() => { daemonRef.current = daemon }, [daemon])
  const engineRef = useRef<'naumi' | 'pi'>('naumi')
  useEffect(() => { engineRef.current = engine }, [engine])
  const busyRef = useRef(false)
  useEffect(() => { busyRef.current = busy }, [busy])
  useEffect(() => { sessionsRef.current = sessions }, [sessions])

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
    sessionsRef.current = all
    setSessions(all)
    return all
  }, [api])
  const fetchAllMessages = useCallback(async (id: string) => {
    const first = await api.fetchMessages(id, 1, 200)
    let allMessages = first.messages
    for (let page = 2; allMessages.length < first.total; page++) {
      const next = await api.fetchMessages(id, page, 200)
      if (!next.messages.length) break
      allMessages = [...allMessages, ...next.messages]
    }
    return allMessages
  }, [api])

  const select = useCallback(
    async (id: string | null) => {
      const target = id ? sessionsRef.current.find((item) => item.id === id) : undefined
      const currentWorkspace = daemonRef.current?.workspace_root
      if (
        target?.workspace_root &&
        currentWorkspace &&
        !workspacePathsMatch(target.workspace_root, currentWorkspace)
      ) {
        setError(`该会话属于 ${target.workspace_root}，请先切换到对应项目`)
        return
      }
      const current = ++generation.current
      activeId.current = id
      setSessionId(id)
      setEngineState(target?.engine === 'pi' ? 'pi' : 'naumi')
      savePreference('session', id ?? '')
      setMessages([])
      setSources([])
      setSelectedSources([])
      setRuns([])
      setDiff(null)
      diffRevision.current++
      setDiffError(''); setDiffLoading(false); setDiffUpdatedAt('')
      setSnapshot(null)
      snapshotRevision.current++
      setSnapshotLoading(false)
      setSnapshotError('')
      setPermissions((previous) => previous.filter(
        (permission) => permission.sessionId === runningSessionId.current,
      ))
      setLiveEvents([])
      setRunningUserMessageId(null)
      setError('')
      setFailedSend(null)
      setDraftState(readPreference(`draft:${id ?? 'new'}`))
      if (!id) {
        setLoading(false)
        return
      }
      setLoading(true)
      const results = await Promise.allSettled([
        fetchAllMessages(id),
        api.fetchChatEnvironment(id),
        api.runs(id),
        api.fetchSnapshot(id),
      ])
      if (current !== generation.current) return
      const [history, environment, records, state] = results
      if (history.status === 'fulfilled') {
        setMessages(history.value)
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
    [api, fetchAllMessages],
  )

  const connect = useCallback(async () => {
    const current = ++connectionGeneration.current
    setConnecting(true)
    setError('')
    try {
      const status = await api.fetchDaemonStatus()
      if (current !== connectionGeneration.current) return
      setDaemon(status)
      daemonRef.current = status
      api.commands().then(result => {
        if (current === connectionGeneration.current) { setCommands(result.commands ?? []); setCommandsError('') }
      }).catch(() => { if (current === connectionGeneration.current) setCommandsError('命令列表未加载，请重新连接') })
      api.engines().then(result => {
        if (current !== connectionGeneration.current) return
        setEngines(result.engines ?? [])
        if (!readPreference('engine')) setEngineState(result.default === 'pi' ? 'pi' : 'naumi')
      }).catch(() => {})
      const loadedSessions = await refreshSessions()
      if (current !== connectionGeneration.current) return
      const selected = readPreference('session')
      const selectedSession = loadedSessions.find((item) => item.id === selected)
      if (
        selectedSession &&
        (
          !selectedSession.workspace_root ||
          workspacePathsMatch(selectedSession.workspace_root, status.workspace_root)
        )
      ) {
        await select(selected)
      } else if (selected) {
        savePreference('session', '')
        await select(null)
      }
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
        daemonRef.current = null
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
        const url = buildWorkbenchEventStreamUrl(
          daemon.event_stream_url_template,
          id,
          platform.supportsDaemon ? undefined : location.origin,
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

  const switchEngine = useCallback(
    (next: 'naumi' | 'pi') => {
      if (next === engineRef.current) return
      if (busyRef.current) {
        setError('任务运行中，请等待完成后再切换引擎。')
        return
      }
      setEngineState(next)
      savePreference('engine', next)
      void select(null)
    },
    [select],
  )

  const ensureSession = async () => {
    if (activeId.current) return activeId.current
    const current = generation.current
    if (!creatingSession.current)
      creatingSession.current = api.create(undefined, model || undefined, engineRef.current)
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
  const send = async (
    contentOverride?: string,
    retryUserMessageId?: string,
    replaceAssistantMessageId?: string,
    editUserMessageId?: string,
    persistedEditMessageId?: string,
  ): Promise<boolean> => {
    const requestedContent = contentOverride ?? draft
    if (!requestedContent.trim() || operation.current || !daemon || loading) return false
    const editIndex = editUserMessageId
      ? messages.findIndex((message) => message.id === editUserMessageId && message.role === 'user')
      : -1
    if (editUserMessageId && editIndex < 0) {
      setError('要编辑的消息已不存在，请刷新会话后重试')
      return false
    }
    operation.current = true
    stopped.current = false
    setBusy(true)
    setError('')
    setFailedSend(null)
    const sendGeneration = generation.current
    const content = requestedContent.trim()
    const messageId = replaceAssistantMessageId ?? `stream-${crypto.randomUUID()}`
    const optimisticUserId = editUserMessageId ?? retryUserMessageId ?? `user-${messageId}`
    const editingBranch = Boolean(editUserMessageId)
    const messageSnapshot = messages
    const replacingAssistant = Boolean(replaceAssistantMessageId)
    const replacedAssistant = replaceAssistantMessageId
      ? messages.find((message) => message.id === replaceAssistantMessageId)
      : undefined
    const assistantSnapshot = new Map(
      messages
        .filter((message) => message.role === 'assistant')
        .map((message) => [message.id, message.content]),
    )
    const startedAt = new Date().toISOString()
    let completed = false
    let failure = ''
    let receivedAssistantContent = false
    let succeeded = false
    let sessionReady = false
    let sentSessionId = ''
    setRunningUserMessageId(optimisticUserId)
    setMessages((previous) => {
      if (editUserMessageId) {
        const currentIndex = previous.findIndex((message) => message.id === editUserMessageId)
        if (currentIndex < 0) return previous
        return [
          ...previous.slice(0, currentIndex),
          {
            ...previous[currentIndex],
            content,
            timestamp: startedAt,
            metadata: { ...previous[currentIndex].metadata, pending: true },
          },
        ]
      }
      const next = retryUserMessageId
        ? previous.map((message) =>
            message.id === retryUserMessageId
              ? { ...message, metadata: { ...message.metadata, pending: true } }
              : message,
          )
        : [
            ...previous,
            {
              id: optimisticUserId,
              role: 'user',
              content,
              timestamp: new Date().toISOString(),
              metadata: { pending: true },
            },
          ]
      return replaceAssistantMessageId
        ? next.map((message) =>
            message.id === replaceAssistantMessageId
              ? {
                  ...message,
                  content: '',
                  timestamp: startedAt,
                  metadata: { ...message.metadata, pending: true },
                }
              : message,
          )
        : next
    })
    if (!replacingAssistant && !editingBranch) setDraft('')
    setLiveEvents([{
      id: `local-${messageId}`,
      type: 'turn_start',
      turn: 1,
      sequence: 0,
      timestamp: new Date().toISOString(),
      data: { local: true },
    }])
    try {
      const id = await ensureSession()
      sentSessionId = id
      runningSessionId.current = id
      setVisibleRunningSessionId(id)
      sessionReady = true
      if (!replacingAssistant && !editingBranch) savePreference(`draft:${id}`, '')
      if (stopped.current) return false
      controller.current = new AbortController()
      runId.current = ''
      await api.stream(
        id,
        {
          content,
          runtime_mode: mode,
          edit_message_id: persistedEditMessageId ?? editUserMessageId,
          source_ids: replacingAssistant || editingBranch ? [] : selectedSources,
          workbench_issue:
            !replacingAssistant && !editingBranch && createIssue && snapshot?.missions[0]
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
          if (activeId.current !== id) return
          if (event.type === 'token_delta') {
            const token = String(event.data.token ?? event.data.content ?? '')
            if (token) receivedAssistantContent = true
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
          if (isTimelineEvent(event)) {
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
                        sessionId: id,
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
      if (!receivedAssistantContent && !stopped.current) {
        const persistedMessages = await fetchAllMessages(id)
        const finalMessage = persistedMessages.at(-1)
        if (
          finalMessage?.role === 'assistant' &&
          finalMessage.content.trim() &&
          (
            !assistantSnapshot.has(finalMessage.id) ||
            assistantSnapshot.get(finalMessage.id) !== finalMessage.content
          )
        ) {
          receivedAssistantContent = true
          if (activeId.current === id) setMessages(persistedMessages)
        }
      }
      if (!receivedAssistantContent)
        throw new Error('任务结束但未返回可显示结果，请重试')
      if (!replacingAssistant && !editingBranch) setSelectedSources([])
      succeeded = true
      if (editingBranch && activeId.current === id) {
        try {
          setMessages(await fetchAllMessages(id))
        } catch (e) {
          setError(`修改已完成，但会话刷新失败：${errorText(e)}`)
        }
      }
      setMessages((previous) =>
        previous.map((message) =>
          message.id === optimisticUserId || message.id === messageId
            ? { ...message, metadata: { ...message.metadata, pending: false } }
            : message,
        ),
      )
    } catch (e) {
      const remainsActive =
        sendGeneration === generation.current &&
        (!sentSessionId || activeId.current === sentSessionId)
      if (!stopped.current && remainsActive) {
        const message = errorText(e)
        if (!sessionReady)
          setMessages(previous => previous.filter(message => message.id !== optimisticUserId))
        if (editingBranch)
          setMessages(messageSnapshot)
        if (replacedAssistant)
          setMessages(previous => previous.map(message =>
            message.id === replaceAssistantMessageId ? replacedAssistant : message,
          ))
        setError(message)
        if (!editingBranch) {
          setFailedSend({
            content,
            error: message,
            userMessageId: sessionReady ? optimisticUserId : undefined,
            assistantMessageId: replaceAssistantMessageId,
          })
        }
        if (!replacingAssistant && !editingBranch) setDraft(content)
      }
    } finally {
      controller.current = null
      setPermissions([])
      if (
        !succeeded &&
        stopped.current &&
        replacedAssistant &&
        sendGeneration === generation.current
      ) {
        setMessages(previous => previous.map(message =>
          message.id === replaceAssistantMessageId ? replacedAssistant : message,
        ))
      }
      if (sentSessionId) {
        const results = await Promise.allSettled([
          api.runs(sentSessionId),
          refreshSessions(),
        ])
        if (
          results[0].status === 'fulfilled' &&
          activeId.current === sentSessionId
        ) setRuns(results[0].value.runs)
        for (const result of results)
          if (result.status === 'rejected')
            setError(
              (previous) =>
                previous || `执行记录更新失败：${errorText(result.reason)}`,
            )
      }
      setBusy(false)
      setRunningUserMessageId(null)
      runningSessionId.current = null
      setVisibleRunningSessionId(null)
      operation.current = false
      void taskState.refreshTasks()
      void goalState.refreshGoals()
    }
    return succeeded
  }
  const retryFailedSend = async () => {
    const failed = failedSend
    if (!failed || failed.error !== error || operation.current || !daemon || loading) return
    await send(failed.content, failed.userMessageId, failed.assistantMessageId)
  }
  const regenerate = async (
    content: string,
    userMessageId: string,
    assistantMessageId: string,
  ) => send(content, userMessageId, assistantMessageId)
  const reviseMessage = async (content: string, userMessageId: string) => {
    const id = activeId.current
    const currentIndex = messages.findIndex(
      message => message.id === userMessageId && message.role === 'user',
    )
    if (!id || currentIndex < 0) {
      setError('要编辑的消息已不存在，请刷新会话后重试')
      return false
    }
    const userOrdinal = messages
      .slice(0, currentIndex + 1)
      .filter(message => message.role === 'user').length - 1
    try {
      const persistedMessages = await fetchAllMessages(id)
      const persistedUser = persistedMessages
        .filter(message => message.role === 'user')[userOrdinal]
      if (!persistedUser) {
        setError('要编辑的消息未能从会话历史中恢复，请刷新后重试')
        return false
      }
      return send(
        content,
        undefined,
        undefined,
        userMessageId,
        persistedUser.id,
      )
    } catch (e) {
      setError(`无法准备消息编辑：${errorText(e)}`)
      return false
    }
  }
  const stop = async () => {
    stopped.current = true
    try {
      if (runningSessionId.current && runId.current)
        await api.cancel(runningSessionId.current, runId.current)
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
    const revision = ++diffRevision.current
    setDiffLoading(true)
    setDiffError('')
    try {
      const id = await ensureSession()
      const next = await api.fetchGitDiff(id)
      if (current === generation.current && activeId.current === id && revision === diffRevision.current) {
        setDiff(next); setDiffUpdatedAt(new Date().toISOString())
      }
    } catch (e) {
      if (current === generation.current && revision === diffRevision.current) setDiffError(errorText(e))
    } finally {
      if (revision === diffRevision.current) setDiffLoading(false)
    }
  }
  const refreshSnapshot = async () => {
    const id = activeId.current
    if (!id || !daemon) return
    const current = generation.current
    const revision = ++snapshotRevision.current
    setSnapshotLoading(true)
    setSnapshotError('')
    try {
      const next = await api.fetchSnapshot(id)
      if (current === generation.current && revision === snapshotRevision.current) setSnapshot(next)
    } catch (error) {
      if (current === generation.current && revision === snapshotRevision.current) setSnapshotError(errorText(error))
    } finally {
      if (revision === snapshotRevision.current) setSnapshotLoading(false)
    }
  }
  const changeModel = async (value: string) => {
    if (mutating) return
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
      setMutating(false)
    }
  }
  const deleteSession = async (id: string) => {
    if (mutating) return
    setMutating(true)
    try {
      const result = (await api.deleteSession(id)) as
        { status?: string } | undefined
      if (result?.status === 'retry_scheduled')
        throw new Error('会话正在清理中，请稍后刷新')
      setSessions((previous) => previous.filter((item) => item.id !== id))
      if (id === activeId.current) await select(null)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setMutating(false)
    }
  }
  const renameSession = async (id: string, title: string) => {
    const normalized = title.trim()
    if (!normalized || mutating) return false
    setMutating(true)
    try {
      const updated = await api.updateSession(id, { title: normalized })
      setSessions(previous => previous.map(item => item.id === id ? updated : item))
      return true
    } catch (e) {
      setError(errorText(e))
      return false
    } finally {
      setMutating(false)
    }
  }
  const pinSession = async (id: string, pinned: boolean) => {
    if (mutating) return false
    setMutating(true)
    try {
      const updated = await api.pinSession(id, pinned)
      setSessions(previous => [
        updated,
        ...previous.filter(item => item.id !== id),
      ].sort((a, b) => Number(b.pinned) - Number(a.pinned)))
      return true
    } catch (e) {
      setError(errorText(e))
      return false
    } finally {
      setMutating(false)
    }
  }
  const archiveSession = async (id: string) => {
    if (mutating) return false
    setMutating(true)
    try {
      await api.archiveSession(id)
      setSessions(previous => previous.filter(item => item.id !== id))
      if (activeId.current === id) await select(null)
      return true
    } catch (e) {
      setError(errorText(e))
      return false
    } finally {
      setMutating(false)
    }
  }
  const duplicateSession = async (id: string) => {
    if (mutating) return null
    setMutating(true)
    try {
      const duplicate = await api.duplicateSession(id)
      setSessions(previous => [duplicate, ...previous])
      await select(duplicate.id)
      return duplicate
    } catch (e) {
      setError(errorText(e))
      return null
    } finally {
      setMutating(false)
    }
  }
  const createSidebarSession = async () => {
    if (!daemon || mutating) return null
    setMutating(true)
    try {
      const created = await api.create('新对话', model || undefined)
      setSessions(previous => [created, ...previous])
      return created
    } catch (e) {
      setError(errorText(e))
      return null
    } finally {
      setMutating(false)
    }
  }
  const resolve = async (
    permission: Permission,
    decision: 'allow' | 'deny',
  ) => {
    if (!permission.sessionId) return
    try {
      await api.resolvePermission(permission.sessionId, permission.callId, { decision })
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
        daemonRef.current = null
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
  const goalState = useWorkspaceGoals(api, !!daemon, ensureSession)
  return {
    ...goalState,
    ...taskState,
    api,
    base,
    daemon,
    config,
    commands,
    commandsError,
    sessions,
    sessionId,
    messages,
    sources,
    selectedSources,
    setSelectedSources,
    runs,
    snapshot,
    snapshotLoading,
    snapshotError,
    refreshSnapshot,
    diff,
    diffLoading,
    diffError,
    diffUpdatedAt,
    error,
    setError,
    failedMessage: failedSend?.error === error ? failedSend.content : '',
    retryFailedSend,
    connecting,
    loading,
    busy,
    runningUserMessageId,
    runningSessionId: visibleRunningSessionId,
    uploading,
    draft,
    setDraft,
    model,
    engine,
    engines,
    switchEngine,
    mode,
    sendKey,
    setSendKey,
    setMode,
    permissions: permissions.filter((permission) => permission.sessionId === sessionId),
    liveEvents,
    select,
    connect,
    send,
    regenerate,
    reviseMessage,
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
    renameSession,
    pinSession,
    archiveSession,
    duplicateSession,
    createSidebarSession,
  }
}
