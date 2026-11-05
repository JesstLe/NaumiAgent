import { useEffect, useRef, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
export function FollowupDialog({ title, dirty, busy, onClose, onStopWaiting, children }: { title: string; dirty: boolean; busy: boolean; onClose: () => void; onStopWaiting?: () => void; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null), close = useRef(onClose), released = useRef(false), [leave, setLeave] = useState<ArtifactNavigation | null>(null); close.current = onClose
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null, node = ref.current
    node?.querySelector<HTMLElement>('input,select,textarea,button')?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); requestArtifactNavigation(() => close.current()) }
      if (event.key !== 'Tab' || !node) return
      const fields = [...node.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')].filter(field => field.tabIndex >= 0)
      if (fields.length && (event.shiftKey ? document.activeElement === fields[0] : document.activeElement === fields.at(-1))) { event.preventDefault(); (event.shiftKey ? fields.at(-1) : fields[0])?.focus() }
    }
    node?.addEventListener('keydown', key); return () => { node?.removeEventListener('keydown', key); if (previous?.isConnected) previous.focus() }
  }, [])
  useEffect(() => {
    const guard = (event: Event) => { if (released.current || !dirty && !busy) return; event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail) }
    const unload = (event: BeforeUnloadEvent) => { if (dirty || busy) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty, busy])
  return <div className="pf-backdrop"><section className="pf-dialog" ref={ref} role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2>{busy && onStopWaiting && <button type="button" onClick={onStopWaiting}>停止等待</button>}<button disabled={busy} aria-label="关闭跟进表单" onClick={() => requestArtifactNavigation(onClose)}><X size={18} /></button></header>{leave && <section className="pf-notice" role="alertdialog" aria-label="保留未保存跟进"><strong>{busy ? '正在核对原请求' : '保留未保存的修改？'}</strong><div className="pf-actions"><button onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button>{!busy && <button onClick={() => { released.current = true; const next = leave; setLeave(null); onClose(); next.proceed() }}>放弃并离开</button>}</div></section>}{children}</section></div>
}
