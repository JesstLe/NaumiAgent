import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Loader2, Search, X } from 'lucide-react'
import type { TodayEntry } from '@naumi/shared/api/personalToday'
import { todayError, validateTodayEntry, type PersonalTodayApi } from './usePersonalToday'

export function TodaySourcePicker({ api, workspaceKey, namespace, included, locked, onClose, onSelect }: {
  api: PersonalTodayApi; workspaceKey: string; namespace: string; included: string[]; locked: boolean
  onClose: () => void; onSelect: (entry: TodayEntry) => Promise<boolean>
}) {
  const [entries, setEntries] = useState<TodayEntry[]>([]), [after, setAfter] = useState<string | null>(null)
  const [search, setSearch] = useState(''), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  const sequence = useRef(0)
  useEffect(() => {
    const controller = new AbortController(), run = ++sequence.current
    setEntries([]); setAfter(null); setError(''); setLoading(true)
    void api.getPersonalTodaySources({ limit: 50 }, controller.signal).then(page => {
      if (controller.signal.aborted || run !== sequence.current) return
      const values = page.entries.map(entry => validateTodayEntry(entry, workspaceKey))
      setEntries(values); setAfter(page.next_after)
    }).catch(cause => { if (!controller.signal.aborted && run === sequence.current) { setEntries([]); setError(todayError(cause)) } })
      .finally(() => { if (!controller.signal.aborted && run === sequence.current) setLoading(false) })
    return () => { controller.abort(); sequence.current++ }
  }, [api, namespace, workspaceKey])
  const more = async () => {
    if (!after || loading) return
    const run = sequence.current; setLoading(true)
    try {
      const page = await api.getPersonalTodaySources({ limit: 50, after })
      if (run !== sequence.current) return
      const values = page.entries.map(entry => validateTodayEntry(entry, workspaceKey))
      setEntries(previous => [...previous, ...values.filter(entry => !previous.some(old => old.item_ref.item_id === entry.item_ref.item_id))]); setAfter(page.next_after)
    } catch (cause) { if (run === sequence.current) { setEntries([]); setError(todayError(cause)) } }
    finally { if (run === sequence.current) setLoading(false) }
  }
  const visible = entries.filter(entry => entry.availability === 'available' && entry.title?.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  return <section className="pt-overlay" role="dialog" aria-modal="true" aria-label="选择真实事项"><div className="pt-picker">
    <header><h2>加入安排</h2><button aria-label="关闭事项选择" onClick={onClose}><X size={18} /></button></header>
    <label className="pt-search"><Search size={16} /><input autoFocus aria-label="搜索可安排事项" value={search} onChange={event => setSearch(event.target.value)} /></label>
    {error && <div className="pt-alert" role="alert">{error}</div>}
    <div className="pt-picker-list">{visible.map(entry => <button key={entry.item_ref.item_id} disabled={locked || included.includes(entry.metadata_id)} onClick={() => void onSelect(entry).then(saved => { if (saved) onClose() })}>
      <div><strong>{entry.title}</strong><span>{entry.assignee?.name}</span></div><span>{included.includes(entry.metadata_id) ? '已安排' : <ArrowRight size={17} />}</span>
    </button>)}{!loading && !visible.length && <div className="pt-empty">没有可安排的事项</div>}</div>
    <footer>{loading && <span role="status"><Loader2 size={15} className="pa-spin" />正在核对事项</span>}{after && <button disabled={loading || locked} onClick={() => void more()}>更多事项</button>}</footer>
  </div></section>
}
