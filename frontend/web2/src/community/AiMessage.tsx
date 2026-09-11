import { useState, type ReactNode } from 'react'
import { Check, Copy, RotateCcw, ThumbsDown, ThumbsUp } from 'lucide-react'
import { AgentAvatar } from './AgentAvatar'
import './community.css'

type Vote = 'up' | 'down'

export function AiMessage({
  from,
  timestamp,
  seed,
  children,
  copyText,
  onCopyError,
  onRetry,
  onVote,
}: {
  from: 'user' | 'assistant'
  timestamp?: string
  seed: string
  children: ReactNode
  copyText: string
  onCopyError?: () => void
  onRetry?: () => void
  onVote?: (vote: Vote) => void
}) {
  const [copied, setCopied] = useState(false)
  const [vote, setVote] = useState<Vote | null>(null)
  const label = from === 'assistant' ? 'NaumiAgent' : '你'
  return <article className={`w2-message community-message ${from}`}>
    {from === 'assistant' && <AgentAvatar seed={seed} size={27} />}
    <div className="community-message-body">
      <header>
        <strong>{label}</strong>
        {timestamp && <time dateTime={timestamp}>{new Date(timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>}
      </header>
      <div className="community-message-content">{children}</div>
      <div className="community-message-actions" aria-label={`${label}消息操作`}>
        <button type="button" title={copied ? '已复制' : '复制'} aria-label={copied ? '已复制' : '复制消息'} onClick={() => {
          void navigator.clipboard.writeText(copyText).then(() => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1600)
          }).catch(() => onCopyError?.())
        }}>{copied ? <Check /> : <Copy />}</button>
        {onRetry && <button type="button" title="重新生成" aria-label="重新生成" onClick={onRetry}><RotateCcw /></button>}
        {onVote && <>
          <button type="button" title="有帮助" aria-label="有帮助" aria-pressed={vote === 'up'} onClick={() => { setVote('up'); onVote('up') }}><ThumbsUp /></button>
          <button type="button" title="没有帮助" aria-label="没有帮助" aria-pressed={vote === 'down'} onClick={() => { setVote('down'); onVote('down') }}><ThumbsDown /></button>
        </>}
      </div>
    </div>
  </article>
}
