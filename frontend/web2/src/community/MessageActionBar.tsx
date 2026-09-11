import { useState } from 'react'
import { Check, Copy, RotateCcw, ThumbsDown, ThumbsUp } from 'lucide-react'
import { readPreference, savePreference } from '@naumi/shared/api/WorkbenchRuntimeClient'
import './community.css'

type Vote = 'up' | 'down'

function timeLabel(timestamp?: string) {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function MessageActionBar({
  messageId,
  timestamp,
  text,
  onRetry,
  retryDisabled = false,
  onCopyError,
}: {
  messageId: string
  timestamp?: string
  text: string
  onRetry: () => void
  retryDisabled?: boolean
  onCopyError: () => void
}) {
  const storageKey = `message-vote:${messageId}`
  const [copied, setCopied] = useState(false)
  const [vote, setVote] = useState<Vote | null>(() => {
    const stored = readPreference(storageKey)
    return stored === 'up' || stored === 'down' ? stored : null
  })
  const chooseVote = (next: Vote) => {
    const value = vote === next ? null : next
    setVote(value)
    savePreference(storageKey, value || '')
  }

  return <div className="community-message-action-bar" aria-label="助手消息操作">
    {timeLabel(timestamp) && <time dateTime={timestamp}>{timeLabel(timestamp)}</time>}
    <button type="button" title={copied ? '已复制' : '复制'} aria-label={copied ? '已复制' : '复制消息'} onClick={() => {
      void navigator.clipboard.writeText(text).then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1600)
      }).catch(onCopyError)
    }}>{copied ? <Check /> : <Copy />}</button>
    <button type="button" title="重新生成" aria-label="重新生成" disabled={retryDisabled} onClick={onRetry}><RotateCcw /></button>
    <button type="button" title="有帮助" aria-label="有帮助" aria-pressed={vote === 'up'} onClick={() => chooseVote('up')}><ThumbsUp /></button>
    <button type="button" title="没有帮助" aria-label="没有帮助" aria-pressed={vote === 'down'} onClick={() => chooseVote('down')}><ThumbsDown /></button>
  </div>
}
