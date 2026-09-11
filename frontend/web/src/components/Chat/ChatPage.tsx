import { useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Paperclip, Send, Square } from 'lucide-react'
import { useWorkspace } from '@/hooks/WorkspaceProvider'
import { MessageBubble } from './MessageBubble'

// The legacy shell is a presentation adapter over the shared workspace controller.
export function ChatPage() {
  const { t } = useTranslation()
  const w = useWorkspace()
  const file = useRef<HTMLInputElement>(null)
  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="p-4 border-b border-neutral-200 flex justify-between">
        <span>
          {w.sessions.find((s) => s.id === w.sessionId)?.title ||
            t('chat.title')}
        </span>
        <button
          disabled={w.busy || w.uploading}
          onClick={() => void w.select(null)}
        >
          {t('session.new')}
        </button>
      </header>
      {w.error && (
        <div role="alert" className="p-3 bg-amber-50 text-amber-900">
          {w.error}
        </div>
      )}
      <div className="flex-1 overflow-y-auto p-6 space-y-4 bg-neutral-50">
        {w.loading && <p>正在加载会话…</p>}
        {w.messages
          .filter((m) => ['user', 'assistant'].includes(m.role) && m.content)
          .map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
        {w.busy && <p role="status">正在执行…</p>}
      </div>
      <div className="p-4 border-t border-neutral-200 space-y-3">
        {w.permissions.map((p) => (
          <div key={p.callId} className="p-3 bg-amber-50">
            <p>
              {p.name}：{p.reason}
            </p>
            <button onClick={() => void w.resolve(p, 'allow')}>允许本次</button>{' '}
            · <button onClick={() => void w.resolve(p, 'deny')}>拒绝</button>
          </div>
        ))}
        <div className="flex gap-3">
          <select
            aria-label="模型"
            disabled={w.busy}
            value={w.model}
            onChange={(e) => void w.changeModel(e.target.value)}
          >
            {!w.config?.models.some((m) => m.id === w.model) && (
              <option value={w.model}>{w.model || '默认模型'}</option>
            )}
            {w.config?.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
          <select
            aria-label="执行模式"
            value={w.mode}
            disabled={w.busy}
            onChange={(e) => w.setMode(e.target.value as typeof w.mode)}
          >
            <option value="default">默认权限</option>
            <option value="plan">计划模式</option>
            <option value="bypass">跳过审批</option>
          </select>
        </div>
        <textarea
          value={w.draft}
          onChange={(e) => w.setDraft(e.target.value)}
          disabled={w.busy}
          placeholder={t('chat.composerPlaceholder')}
          className="w-full min-h-24 resize-none border border-neutral-200 rounded-md p-3"
          onKeyDown={(e) => {
            if (
              e.key === 'Enter' &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              e.keyCode !== 229
            ) {
              e.preventDefault()
              void w.send()
            }
          }}
        />
        <div className="flex gap-2 flex-wrap">
          {w.sources.map((source) => (
            <label key={source.id}>
              <input
                type="checkbox"
                disabled={w.busy}
                checked={w.selectedSources.includes(source.id)}
                onChange={(e) =>
                  w.setSelectedSources(
                    e.target.checked
                      ? [...w.selectedSources, source.id]
                      : w.selectedSources.filter((id) => id !== source.id),
                  )
                }
              />{' '}
              {source.title}
            </label>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <input
            type="file"
            ref={file}
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) void w.upload(e.target.files)
              e.target.value = ''
            }}
          />
          <button
            title="上传附件"
            disabled={w.busy || w.uploading || !w.daemon}
            onClick={() => file.current?.click()}
          >
            <Paperclip size={18} />
          </button>
          <label className="flex-1">
            <input
              type="checkbox"
              checked={w.createIssue}
              disabled={w.busy || !w.snapshot?.missions.length}
              onChange={(e) => w.setCreateIssue(e.target.checked)}
            />{' '}
            {t('action.createLinkedIssue')}
          </label>
          <button
            className="px-4 py-2 bg-blue-600 text-white rounded-md disabled:opacity-40"
            disabled={
              !w.busy &&
              (!w.draft.trim() || w.uploading || !w.daemon || w.loading)
            }
            onClick={() => void (w.busy ? w.stop() : w.send())}
          >
            {w.busy ? <Square size={16} /> : <Send size={16} />}
            {w.busy ? '停止执行' : t('action.send')}
          </button>
        </div>
      </div>
    </div>
  )
}
