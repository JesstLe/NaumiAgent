import { useId, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, ExternalLink, Link2 } from 'lucide-react'
import type { MessageResponse } from '@naumi/shared/api/types'
import './community.css'

export interface AiSource {
  id: string
  title: string
  url: string
  snippet?: string
}

function safeUrl(value: unknown): string {
  if (typeof value !== 'string') return ''
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch {
    return ''
  }
}

function structuredSources(metadata: Record<string, unknown>): AiSource[] {
  const candidates = [metadata.sources, metadata.citations, metadata.references]
    .find(Array.isArray) as unknown[] | undefined
  if (!candidates) return []
  return candidates.flatMap((candidate, index) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return []
    const item = candidate as Record<string, unknown>
    const url = safeUrl(item.url ?? item.href)
    if (!url) return []
    const title = typeof item.title === 'string' && item.title.trim()
      ? item.title.trim().slice(0, 180)
      : new URL(url).hostname
    const snippet = typeof item.snippet === 'string'
      ? item.snippet.trim().slice(0, 600)
      : typeof item.description === 'string'
        ? item.description.trim().slice(0, 600)
        : undefined
    return [{ id: String(item.id || `source-${index}-${url}`), title, url, snippet }]
  })
}

function linkedSources(content: string): AiSource[] {
  const markdown = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g
  const bare = /https?:\/\/[^\s<>)\]]+/g
  const matches: { title?: string; url: string; index: number }[] = []
  for (const match of content.matchAll(markdown)) matches.push({ title: match[1], url: match[2], index: match.index })
  for (const match of content.matchAll(bare)) matches.push({ url: match[0], index: match.index })
  const seen = new Set<string>()
  return matches.sort((a, b) => a.index - b.index).flatMap((match, index) => {
    const url = safeUrl(match.url.replace(/[.,;:!?，。；：！？]+$/, ''))
    if (!url || seen.has(url)) return []
    seen.add(url)
    const start = Math.max(0, match.index - 80)
    const end = Math.min(content.length, match.index + match.url.length + 100)
    const snippet = content.slice(start, end).replace(/\s+/g, ' ').trim()
    return [{
      id: `link-${index}-${url}`,
      title: match.title?.trim().slice(0, 180) || new URL(url).hostname,
      url,
      snippet: snippet === match.url ? undefined : snippet,
    }]
  })
}

export function messageSources(message: MessageResponse): AiSource[] {
  const structured = structuredSources(message.metadata || {})
  return structured.length ? structured : linkedSources(message.content)
}

export function AiSources({ sources, defaultOpen = false, label = '来源' }: { sources: AiSource[]; defaultOpen?: boolean; label?: string }) {
  const [open, setOpen] = useState(defaultOpen)
  const [expanded, setExpanded] = useState<string | null>(null)
  const regionId = useId()
  if (!sources.length) return null
  return <section className="community-sources">
    <button type="button" className="community-sources-trigger" aria-expanded={open} aria-controls={regionId} onClick={() => setOpen(value => !value)}>
      <Link2 aria-hidden />
      <span>{label}</span>
      <small>{sources.length}</small>
      <ChevronDown aria-hidden />
    </button>
    <AnimatePresence initial={false}>
      {open && <motion.div
        id={regionId}
        className="community-sources-list"
        initial={{ height: 0, opacity: 0 }}
        animate={{ height: 'auto', opacity: 1 }}
        exit={{ height: 0, opacity: 0 }}
        transition={{ duration: .2, ease: [0.23, 1, 0.32, 1] }}
      >
        {sources.map((source, index) => <article key={source.id}>
          <div>
            <span>{index + 1}</span>
            <a href={source.url} target="_blank" rel="noopener noreferrer">
              <strong>{source.title}</strong>
              <small>{new URL(source.url).hostname}</small>
              <ExternalLink aria-hidden />
            </a>
            {source.snippet && <button type="button" aria-label={`${expanded === source.id ? '收起' : '展开'}来源摘要：${source.title}`} aria-expanded={expanded === source.id} onClick={() => setExpanded(value => value === source.id ? null : source.id)}><ChevronDown aria-hidden /></button>}
          </div>
          <AnimatePresence initial={false}>
            {source.snippet && expanded === source.id && <motion.p initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: .18 }}>{source.snippet}</motion.p>}
          </AnimatePresence>
        </article>)}
      </motion.div>}
    </AnimatePresence>
  </section>
}
