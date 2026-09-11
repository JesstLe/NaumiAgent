import { useEffect, useState, type ReactNode } from 'react'
import { Download } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { ImagePreview } from './MessageContent'

export function PublishedImage({ path, alt }: { path: string; alt: string }) {
  const { api } = useWorkspace()
  const [url, setUrl] = useState(''), [error, setError] = useState(''), [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    setUrl(''); setError('')
    void api.outputAsset(path, controller.signal).then(blob => {
      if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl) }
    }).catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '图片加载失败') })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [api, path, retry])
  if (error) return <span className="rich-image-error" role="status">{error}<button onClick={() => setRetry(retry + 1)}>重试</button></span>
  return url ? <ImagePreview src={url} alt={alt} /> : <span role="status" className="rich-caption">正在加载图片…</span>
}

export function PublishedFile({ path, children }: { path: string; children?: ReactNode }) {
  const { api } = useWorkspace()
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const download = async () => {
    setBusy(true); setError('')
    try {
      const blob = await api.outputAsset(path)
      const url = URL.createObjectURL(blob), link = document.createElement('a')
      link.href = url
      const ext = path.split('.').pop()
      const label = typeof children === 'string' ? children.replace(/[\\/:*?"<>|]/g, '_').slice(0, 120) : '下载文件'
      link.download = label.endsWith(`.${ext}`) ? label : `${label}.${ext}`
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { setError(e instanceof Error ? e.message : '下载失败，请重试') }
    finally { setBusy(false) }
  }
  return <span className="rich-file"><button onClick={() => { void download() }} disabled={busy}><Download size={15} />{busy ? '正在下载…' : children}</button>{error && <span role="status">{error}</span>}</span>
}
