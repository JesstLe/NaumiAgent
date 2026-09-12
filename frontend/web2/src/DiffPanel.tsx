import { useEffect, useRef, useState } from 'react'
import { File, RefreshCw, Search } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { UnifiedDiff } from './UnifiedDiff'

const stageName: Record<string, string> = { staged: '已暂存', unstaged: '未暂存', untracked: '未跟踪' }
export function DiffPanel() {
  const w = useWorkspace()
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState('all')
  const [split, setSplit] = useState(false)
  const [openFiles, setOpenFiles] = useState<Set<string>>(new Set())
  const load = useRef(w.loadDiff)
  const loading = useRef(w.diffLoading)
  const refreshing = useRef(false)
  load.current = w.loadDiff; loading.current = w.diffLoading
  const refresh = async () => {
    if (loading.current || refreshing.current) return
    refreshing.current = true
    try { await load.current() }
    finally { refreshing.current = false }
  }
  useEffect(() => {
    if (!w.daemon || w.connecting || w.loading) return
    void refresh()
    const timer = setInterval(() => { if (!document.hidden) void refresh() }, 10000)
    const focus = () => { if (!document.hidden) void refresh() }
    window.addEventListener('focus', focus)
    return () => { clearInterval(timer); window.removeEventListener('focus', focus) }
  }, [w.daemon, w.sessionId, w.connecting, w.loading])
  const all = w.diff?.files ?? []
  const files = all.filter(file => (scope === 'all' || file.stage === scope) && file.path.toLowerCase().includes(query.toLowerCase()))
  return <div className="w2-diff-panel">
    <div className="w2-section-heading"><span>{w.diff?.branch || '当前更改'}</span><button aria-label="刷新代码更改" disabled={w.diffLoading} onClick={() => void refresh()}><RefreshCw size={15} className={w.diffLoading ? 'w2-spin' : ''} /></button></div>
    <div className="w2-diff-stats"><span>{all.length} 个文件</span><b>+{all.reduce((sum, file) => sum + file.additions, 0)}</b><em>−{all.reduce((sum, file) => sum + file.deletions, 0)}</em><small role="status">{w.diffLoading ? '更新中…' : w.diffUpdatedAt ? `${new Date(w.diffUpdatedAt).toLocaleTimeString('zh-CN')} 已更新` : ''}</small></div>
    <div className="w2-diff-toolbar"><label><Search size={14} /><input aria-label="筛选更改文件" placeholder="筛选文件…" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <select aria-label="更改范围" value={scope} onChange={event => setScope(event.target.value)}><option value="all">所有更改</option>{Object.entries(stageName).map(([value, name]) => <option key={value} value={value}>{name} ({all.filter(file => file.stage === value).length})</option>)}</select>
    </div>
    <div className="w2-diff-options"><button aria-pressed={!split} onClick={() => setSplit(false)}>统一</button><button aria-pressed={split} onClick={() => setSplit(true)}>并排</button><button disabled={!files.length} onClick={() => setOpenFiles(new Set(files.map(file => `${file.stage}:${file.path}`)))}>全部展开</button><button disabled={!openFiles.size} onClick={() => setOpenFiles(new Set())}>全部折叠</button></div>
    {(w.diffError || w.diff?.error) && <p role="alert" className="w2-error">{w.diffError || w.diff?.error}</p>}
    {!files.length && <p className="w2-muted">{w.diffLoading && !w.diff ? '正在读取 Git 更改…' : !all.length ? w.diff?.available ? '工作区没有未提交的更改' : '当前工作区无法读取 Git 信息' : '没有匹配的文件'}</p>}
    {files.map(file => {
      const key = `${file.stage}:${file.path}`
      const opened = openFiles.has(key)
      return <section className="w2-diff-file" key={key}>
        <button className="w2-diff-file-heading" aria-expanded={opened} onClick={() => setOpenFiles(previous => { const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next })}>
          <File size={14} /><span title={file.path}>{file.path}</span><small>{stageName[file.stage] || file.stage}</small><b>+{file.additions}</b><em>−{file.deletions}</em>
        </button>
        {opened && <>
          {file.patch_notice && <p className="w2-diff-notice" role="status">{file.patch_notice}</p>}
          {!file.patch && !file.patch_truncated ? <p className="w2-muted">无文本补丁（二进制文件或仅元数据变化）</p> : file.patch ? <UnifiedDiff patch={file.patch} split={split} label={`${file.path} 差异`} /> : null}
        </>}
      </section>
    })}
  </div>
}
