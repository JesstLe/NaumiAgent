import { Children, isValidElement, lazy, memo, Suspense, useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import { Check, Copy, Download, X, ZoomIn } from 'lucide-react'
import 'katex/dist/katex.min.css'
import './rich-content.css'

const RichWidget = lazy(() => import('./RichWidget'))

export function contentUrl(value: string): string {
  if (/^\/api\/v1\/output-assets\/[a-f0-9]{64}\.(png|jpg|webp|gif|pdf|csv|txt|json)$/.test(value)) return value
  if (/^#[\w-]+$/.test(value)) return value
  try {
    const url = new URL(value)
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) && !url.username && !url.password ? value : ''
  } catch { return '' }
}

export function downloadContent(content: string, filename: string, type = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function ImagePreview({ src, alt }: { src?: string; alt?: string }) {
  const [failed, setFailed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  if (!src || failed) return <span className="rich-image-error" role="status">{alt || '图片'} · 图片无法加载{src && <button onClick={() => setFailed(false)}>重试</button>}</span>
  return <span className="rich-image">
    <button className="rich-image-open" onClick={() => setExpanded(true)} aria-label={`放大图片：${alt || '图片'}`}>
      <img src={src} alt={alt || '图片'} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      <span><ZoomIn size={15} />查看图片</span>
    </button>
    {alt && <span className="rich-caption">{alt}</span>}
    {expanded && <dialog open className="rich-lightbox" aria-label={alt || '图片预览'} onKeyDown={event => { if (event.key === 'Escape') setExpanded(false) }}>
      <button autoFocus className="rich-close" aria-label="关闭图片" onClick={() => setExpanded(false)}><X /></button>
      <img src={src} alt={alt || '图片'} referrerPolicy="no-referrer" />
      <a href={src} target="_blank" rel="noopener noreferrer">打开原图</a>
    </dialog>}
  </span>
}

export function CodeBlock({ code, language = '' }: { code: string; language?: string }) {
  const [copyState, setCopyState] = useState('复制')
  return <section className="rich-code">
    <header><span>{language || '文本'}</span><div>
      <button onClick={() => { void navigator.clipboard.writeText(code).then(() => setCopyState('已复制')).catch(() => setCopyState('复制失败，请手动选择')) }}>{copyState === '已复制' ? <Check size={14} /> : <Copy size={14} />}{copyState}</button>
      <button aria-label="下载代码" onClick={() => downloadContent(code, `code.${({ python: 'py', javascript: 'js', typescript: 'ts', html: 'html', css: 'css', json: 'json', csv: 'csv', svg: 'svg' } as Record<string, string>)[language] || 'txt'}`)}><Download size={14} /></button>
    </div></header>
    <pre><code>{code}</code></pre>
  </section>
}

function PlainPre({ children }: { children?: ReactNode }) {
  const child = Children.toArray(children)[0]
  if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) return <pre>{children}</pre>
  // Highlighted code is rendered by rehype; toolbar uses plain text from the tree.
  const flatten = (node: ReactNode): string => typeof node === 'string' ? node : Array.isArray(node) ? node.map(flatten).join('') : isValidElement<{ children?: ReactNode }>(node) ? flatten(node.props.children) : ''
  const code = flatten(child.props.children).replace(/\n$/, '')
  const language = child.props.className?.match(/language-([^ ]+)/)?.[1] || ''
  if (language === 'naumi') return <Suspense fallback={<p role="status">正在加载组件…</p>}><RichWidget code={code} /></Suspense>
  return <div className="rich-highlight"><CodeBlock code={code} language={language} /><pre className="rich-highlighted">{children}</pre></div>
}

export const MessageContent = memo(function MessageContent({ content, plain = false }: { content: string; plain?: boolean }) {
  if (plain) return <div className="w2-prose">{content}</div>
  return <div className="rich-content">
    <Markdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { strict: false, trust: false, throwOnError: false, maxExpand: 500 }], [rehypeHighlight, { detect: false }]]}
      skipHtml urlTransform={contentUrl}
      components={{
        a: ({ href, children }) => href ? <a href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
        img: ({ src, alt }) => <ImagePreview key={typeof src === 'string' ? src : ''} src={typeof src === 'string' ? src : ''} alt={alt} />,
        pre: PlainPre,
        table: ({ children }) => <div className="rich-table-scroll"><table>{children}</table></div>,
      }}>{content}</Markdown>
  </div>
})
