import { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, PlusCircle, Loader2, Brain, Cpu, Zap, Paperclip, X } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import type { RuntimeMode, ChatSource } from '@/api/types'

const MODE_ICON: Record<RuntimeMode, typeof Brain> = {
  default: Brain,
  plan: Cpu,
  bypass: Zap,
}

// Supported models exposed by the workbench. In a future phase this list
// should come from the backend capabilities endpoint.
const AVAILABLE_MODELS = [
  { id: 'kimi-for-coding', label: 'Kimi Coding' },
  { id: 'claude-sonnet-4-6', label: 'Claude Sonnet' },
  { id: 'gpt-4o', label: 'GPT-4o' },
]

export function ChatPage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const sessions = useSessionStore((state) => state.sessions)
  const messages = useSessionStore((state) => state.messages)
  const appendMessage = useSessionStore((state) => state.appendMessage)
  const setMessages = useSessionStore((state) => state.setMessages)
  const setError = useSessionStore((state) => state.setError)
  const currentSession = useMemo(
    () => sessions.find((s) => s.id === currentSessionId),
    [sessions, currentSessionId],
  )
  const currentModel = currentSession?.model ?? 'kimi-for-coding'
  const [selectedModel, setSelectedModel] = useState(currentModel)
  const [savingModel, setSavingModel] = useState(false)

  useEffect(() => {
    setSelectedModel(currentModel)
  }, [currentModel])

  const [input, setInput] = useState('')
  const [createIssue, setCreateIssue] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>('default')
  const [sources, setSources] = useState<ChatSource[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!client || !currentSessionId) {
      setMessages([])
      return
    }
    let cancelled = false
    setIsLoading(true)
    client
      .fetchMessages(currentSessionId, 1, 100)
      .then((response) => {
        if (!cancelled) {
          setMessages(response.messages)
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setError(isApiException(error) ? error.message : String(error))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId, setMessages, setError])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (!client || !currentSessionId) {
      setSources([])
      return
    }
    let cancelled = false
    client
      .fetchChatEnvironment(currentSessionId)
      .then((env) => {
        if (!cancelled) setSources(env.sources)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId])

  const handleUpload = async (file: File) => {
    if (!client || !currentSessionId) return
    setUploading(true)
    try {
      const source = await client.uploadChatSource(currentSessionId, file)
      setSources((prev) => [...prev, source])
    } catch (error) {
      setError(isApiException(error) ? error.message : String(error))
    } finally {
      setUploading(false)
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) void handleUpload(file)
    e.target.value = ''
  }

  const handleRemoveSource = (sourceId: string) => {
    setSources((prev) => prev.filter((s) => s.id !== sourceId))
  }

  const handleSend = useCallback(async () => {
    if (!client || !currentSessionId || !input.trim() || isSending) return

    const content = input.trim()
    setInput('')
    setIsSending(true)
    setError(null)

    const userMessage = {
      id: `local-${Date.now()}`,
      role: 'user',
      content,
      timestamp: new Date().toISOString(),
      metadata: {},
    }
    appendMessage(userMessage)

    try {
      const response = await client.sendMessage(currentSessionId, {
        content,
        runtime_mode: runtimeMode,
        source_ids: sources.map((s) => s.id),
        workbench_issue: createIssue
          ? {
              mission_id: snapshot?.missions[0]?.id ?? 'default',
              title: content.slice(0, 80),
              description: content,
            }
          : undefined,
      })
      appendMessage(response)
    } catch (error) {
      setError(isApiException(error) ? error.message : String(error))
    } finally {
      setIsSending(false)
    }
  }, [client, currentSessionId, input, isSending, runtimeMode, createIssue, snapshot, appendMessage, setError])

  const handleModelChange = async (modelId: string) => {
    if (!client || !currentSessionId || savingModel) return
    setSavingModel(true)
    try {
      const updated = await client.updateSession(currentSessionId, { model: modelId })
      setSelectedModel(updated.model)
    } catch (error) {
      setError(isApiException(error) ? error.message : String(error))
    } finally {
      setSavingModel(false)
    }
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void handleSend()
    }
  }

  if (!currentSessionId) {
    return (
      <div className="flex h-full items-center justify-center text-neutral-500">
        {t('chat.emptyState')}
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <header className="px-6 py-3 border-b border-neutral-200 flex items-center justify-between bg-white">
        <div>
          <div className="font-medium text-neutral-900">{snapshot?.summary.current_mission_title || t('chat.title')}</div>
          <div className="text-xs text-neutral-500">{currentSessionId}</div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 rounded-md border border-neutral-200 bg-neutral-50 px-2 py-1 text-xs text-neutral-600">
            <span>{t('chat.model')}:</span>
            {savingModel ? (
              <Loader2 className="w-3 h-3 animate-spin text-neutral-500" />
            ) : (
              <select
                value={selectedModel}
                onChange={(e) => handleModelChange(e.target.value)}
                disabled={savingModel}
                className="bg-transparent font-medium text-neutral-900 focus:outline-none text-xs"
              >
                {AVAILABLE_MODELS.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            )}
          </div>
          <button className="flex items-center gap-1 px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded-md">
            <PlusCircle className="w-4 h-4" /> {t('action.newMission')}
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-6 space-y-4 bg-neutral-50">
        {isLoading && messages.length === 0 && (
          <div className="flex justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-neutral-400" />
          </div>
        )}
        {!isLoading && messages.length === 0 && (
          <div className="text-center text-sm text-neutral-500 py-12">{t('chat.emptyState')}</div>
        )}
        {messages.map((message) => (
          <div
            key={message.id}
            className={`max-w-3xl rounded-lg px-4 py-3 text-sm ${
              message.role === 'user'
                ? 'ml-auto bg-blue-600 text-white'
                : 'bg-white text-neutral-800 shadow-sm'
            }`}
          >
            {message.content}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="p-4 border-t border-neutral-200 bg-white">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs text-neutral-500">{t('chat.thinking')}:</span>
          {(['default', 'plan', 'bypass'] as RuntimeMode[]).map((mode) => {
            const Icon = MODE_ICON[mode]
            const active = runtimeMode === mode
            return (
              <button
                key={mode}
                type="button"
                onClick={() => setRuntimeMode(mode)}
                disabled={isSending}
                className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs border transition-colors ${
                  active
                    ? 'bg-blue-50 border-blue-200 text-blue-700'
                    : 'bg-white border-neutral-200 text-neutral-600 hover:bg-neutral-50'
                } disabled:opacity-50`}
              >
                <Icon className="w-3 h-3" />
                {t(`chat.thinking${mode.charAt(0).toUpperCase() + mode.slice(1)}` as const)}
              </button>
            )
          })}
        </div>
        <div className="flex items-start gap-3">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileChange}
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            title={t('chat.uploadFile')}
            className="flex items-center justify-center w-9 h-9 rounded-md border border-neutral-200 bg-white text-neutral-600 hover:bg-neutral-50 disabled:opacity-50 shrink-0"
          >
            {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Paperclip className="w-4 h-4" />}
          </button>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.composerPlaceholder')}
            disabled={isSending}
            className="flex-1 min-h-[80px] max-h-40 px-3 py-2 rounded-md border border-neutral-200 bg-white resize-none focus:outline-none focus:ring-2 focus:ring-blue-500/50 disabled:opacity-50"
          />
        </div>

        {sources.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            <span className="text-xs text-neutral-500">{t('chat.sources')}:</span>
            {sources.map((source) => (
              <span
                key={source.id}
                className="inline-flex items-center gap-1 rounded-md bg-blue-50 px-2 py-0.5 text-xs text-blue-700 border border-blue-100"
              >
                {source.title}
                <button
                  type="button"
                  onClick={() => handleRemoveSource(source.id)}
                  className="text-blue-400 hover:text-blue-700"
                >
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between mt-3">
          <label className="flex items-center gap-2 text-sm text-neutral-600 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={createIssue}
              onChange={(e) => setCreateIssue(e.target.checked)}
              disabled={isSending}
              className="rounded border-neutral-300 text-blue-600 focus:ring-blue-500"
            />
            {t('action.createLinkedIssue')}
          </label>
          <button
            type="button"
            onClick={() => void handleSend()}
            disabled={isSending || !input.trim()}
            className="flex items-center gap-1 px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700 disabled:opacity-50"
          >
            {isSending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            {t('action.send')}
          </button>
        </div>
      </div>
    </div>
  )
}
