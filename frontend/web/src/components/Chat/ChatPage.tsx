import { useTranslation } from 'react-i18next'
import { Send, PlusCircle } from 'lucide-react'
import { useState } from 'react'

export function ChatPage() {
  const { t } = useTranslation()
  const [input, setInput] = useState('')
  const [createIssue, setCreateIssue] = useState(false)

  return (
    <div className="flex flex-col h-full">
      {/* Session header */}
      <header className="px-6 py-3 border-b border-border flex items-center justify-between bg-panel">
        <div>
          <div className="font-medium text-text">Session #1</div>
          <div className="text-xs text-text-secondary">Default Mission</div>
        </div>
        <button className="flex items-center gap-1 px-3 py-1.5 text-sm text-accent hover:bg-accent/10 rounded-md">
          <PlusCircle className="w-4 h-4" /> {t('action.newMission')}
        </button>
      </header>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        <div className="text-center text-sm text-text-secondary py-12">{t('chat.emptyState')}</div>
      </div>

      {/* Composer */}
      <div className="p-4 border-t border-border bg-panel">
        <div className="flex items-start gap-3">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t('chat.composerPlaceholder')}
            className="flex-1 min-h-[80px] max-h-40 px-3 py-2 rounded-md border border-border bg-bg resize-none focus:outline-none focus:ring-2 focus:ring-accent/50"
          />
        </div>
        <div className="flex items-center justify-between mt-3">
          <label className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer select-none">
            <input
              type="checkbox"
              checked={createIssue}
              onChange={(e) => setCreateIssue(e.target.checked)}
              className="rounded border-border text-accent focus:ring-accent"
            />
            {t('action.createLinkedIssue')}
          </label>
          <button className="flex items-center gap-1 px-4 py-2 bg-accent text-white text-sm rounded-md hover:bg-accent-hover disabled:opacity-50">
            <Send className="w-4 h-4" /> {t('action.send')}
          </button>
        </div>
      </div>
    </div>
  )
}
