import { useEffect, useState, useRef, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, PlusCircle, Loader2, Brain, Cpu, Zap } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import type { RuntimeMode } from '@/api/types'

const MODE_ICON: Record<RuntimeMode, typeof Brain> = {
  default: Brain,
  plan: Cpu,
  bypass: Zap,
}

export function ChatPage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const messages = useSessionStore((state) => state.messages)
  const appendMessage = useSessionStore((state) => state.appendMessage)
  const setMessages = useSessionStore((state) => state.setMessages)
  const setError = useSessionStore((state) => state.setError)
  const [input, setInput] = useState('')
  const [createIssue, setCreateIssue] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>('default')
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
            <span className="font-medium text-neutral-900">{snapshot?.summary.current_mission_title ? 'kimi' : '—'}</span>
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
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.composerPlaceholder')}
            disabled={isSending}
            className="flex-1 min-h-[80px] max-h-40 px-3 py-2 rounded-md border border-neutral-200 bg-white resize-none focus:outline-none focus:ring-2 focus:ring-blue-500/50 disabled:opacity-50"
          />
        </div>
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
