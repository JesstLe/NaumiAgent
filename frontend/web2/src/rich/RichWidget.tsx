import { useMemo, useState } from 'react'
import { BarChart3, Code2, Download, Play, RotateCcw, Square, Table2 } from 'lucide-react'
import { CodeBlock, contentUrl, downloadContent, ImagePreview } from './MessageContent'
import { parseRichSpec, toCsv, type Column, type RichSpec, type Row } from './schema'

const formatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })
const format = (value: unknown) => typeof value === 'number' ? formatter.format(value) : value === null || value === undefined ? '—' : String(value)

function DataTable({ columns, rows }: { columns: Column[]; rows: Row[] }) {
  const [query, setQuery] = useState(''), [page, setPage] = useState(0)
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null)
  const filtered = useMemo(() => {
    const result = rows.filter(row => columns.some(c => String(row[c.key] ?? '').toLowerCase().includes(query.toLowerCase())))
    if (sort) result.sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key]
      if (av == null) return bv == null ? 0 : 1
      if (bv == null) return -1
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv), 'zh-CN', { numeric: true })
      return sort.desc ? -cmp : cmp
    })
    return result
  }, [rows, columns, query, sort])
  const pages = Math.max(1, Math.ceil(filtered.length / 20)), current = Math.min(page, pages - 1)
  const numeric = columns.filter(c => rows.some(r => typeof r[c.key] === 'number'))
  return <>
    <div className="rich-toolbar"><input aria-label="搜索表格" placeholder="搜索数据…" value={query} onChange={e => { setQuery(e.target.value); setPage(0) }} /><span>{filtered.length} / {rows.length} 行</span><button onClick={() => downloadContent(toCsv(columns, filtered), 'data.csv', 'text/csv;charset=utf-8')}><Download size={14} />导出 CSV</button></div>
    <div className="rich-table-scroll"><table><thead><tr>{columns.map(c => <th key={c.key} aria-sort={sort?.key === c.key ? sort.desc ? 'descending' : 'ascending' : 'none'}><button onClick={() => setSort({ key: c.key, desc: sort?.key === c.key && !sort.desc })}>{c.label}{sort?.key === c.key ? sort.desc ? ' ↓' : ' ↑' : ' ↕'}</button></th>)}</tr></thead><tbody>{filtered.slice(current * 20, current * 20 + 20).map((row, i) => <tr key={i}>{columns.map(c => <td key={c.key}>{format(row[c.key])}</td>)}</tr>)}</tbody></table></div>
    {!filtered.length && <p className="rich-empty">{rows.length ? '没有匹配的数据' : '暂无数据'}</p>}
    {pages > 1 && <nav className="rich-pagination" aria-label="表格分页"><button disabled={!current} onClick={() => setPage(current - 1)}>上一页</button><span>{current + 1} / {pages}</span><button disabled={current === pages - 1} onClick={() => setPage(current + 1)}>下一页</button></nav>}
    {numeric.length > 0 && <details className="rich-statistics"><summary>查看数值统计</summary><div>{numeric.map(c => {
      const values = filtered.map(row => row[c.key]).filter((v): v is number => typeof v === 'number')
      return <p key={c.key}><strong>{c.label}</strong><span>有效值 {values.length}</span><span>合计 {values.length ? format(values.reduce((a, b) => a + b, 0)) : '—'}</span><span>均值 {values.length ? format(values.reduce((a, b) => a + b, 0) / values.length) : '—'}</span><span>最小 {values.length ? format(Math.min(...values)) : '—'}</span><span>最大 {values.length ? format(Math.max(...values)) : '—'}</span></p>
    })}</div></details>}
  </>
}

const colors = ['#779163', '#6796ad', '#c89d58', '#a186b2', '#ba7e72', '#649d98']
function Chart({ spec }: { spec: Extract<RichSpec, { type: 'chart' }> }) {
  const [style, setStyle] = useState(spec.style), [table, setTable] = useState(false), [hidden, setHidden] = useState<string[]>([]), [selected, setSelected] = useState<number | null>(null)
  const series = spec.series.filter(s => !hidden.includes(s.key))
  const values = spec.rows.flatMap(row => series.map(s => row[s.key]).filter((v): v is number => typeof v === 'number'))
  const min = Math.min(0, ...values), max = Math.max(0, ...values), extent = max - min || 1
  const x = (i: number) => 62 + (i + .5) * 560 / Math.max(1, spec.rows.length), y = (n: number) => 240 - (n - min) / extent * 200
  return <>
    <div className="rich-toolbar"><div className="rich-segment"><button aria-pressed={!table} onClick={() => setTable(false)}><BarChart3 size={14} />图表</button><button aria-pressed={table} onClick={() => setTable(true)}><Table2 size={14} />数据</button></div><select aria-label="图表类型" value={style} onChange={e => setStyle(e.target.value as typeof style)}><option value="bar">柱状图</option><option value="line">折线图</option><option value="scatter">散点图</option></select></div>
    {table ? <DataTable columns={[{ key: spec.x, label: spec.x }, ...spec.series]} rows={spec.rows} /> : <>
      <div className="rich-legend">{spec.series.map((s, i) => <button key={s.key} aria-pressed={!hidden.includes(s.key)} onClick={() => setHidden(hidden.includes(s.key) ? hidden.filter(k => k !== s.key) : [...hidden, s.key])}><i style={{ background: colors[i] }} />{s.label}</button>)}</div>
      {!spec.rows.length || !values.length ? <p className="rich-empty">{!series.length ? '选择上方系列以显示图表' : '暂无可绘制的数值'}</p> : <>
        <svg className="rich-chart" viewBox="0 0 660 285" role="img" aria-label={spec.title}>
          {[0, 1, 2, 3, 4].map(tick => { const n = min + extent * tick / 4; return <g key={tick}><line x1="62" x2="622" y1={y(n)} y2={y(n)} stroke="#e6ece1" /><text x="54" y={y(n) + 4} textAnchor="end">{format(n)}</text></g> })}
          {series.map((s, si) => {
            const color = colors[spec.series.findIndex(original => original.key === s.key)]
            const d = spec.rows.map((row, i) => typeof row[s.key] === 'number' ? `${i === 0 || typeof spec.rows[i - 1]?.[s.key] !== 'number' ? 'M' : 'L'}${x(i)},${y(row[s.key] as number)}` : '').join(' ')
            return <g key={s.key}>{style === 'line' && <path d={d} fill="none" stroke={color} strokeWidth="2.5" />}{spec.rows.map((row, i) => {
              const value = row[s.key]
              if (typeof value !== 'number') return null
              const width = Math.max(.3, 560 / spec.rows.length * .72 / Math.max(series.length, 1))
              return <g key={i} role="button" tabIndex={0} aria-label={`${row[spec.x]}，${s.label}：${value}`} onMouseEnter={() => setSelected(i)} onFocus={() => setSelected(i)} onClick={() => setSelected(i)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setSelected(i) }}><title>{`${row[spec.x]} · ${s.label}: ${format(value)}`}</title>{style === 'bar' ? <rect x={x(i) - width * series.length / 2 + si * width} y={Math.min(y(value), y(0))} width={width * .9} height={Math.max(1, Math.abs(y(value) - y(0)))} fill={color} rx="1" /> : <circle cx={x(i)} cy={y(value)} r="4" fill={color} />}</g>
            })}</g>
          })}
          {spec.rows.map((row, i) => i % Math.ceil(spec.rows.length / 8) === 0 ? <text key={i} x={x(i)} y="265" textAnchor="middle">{String(row[spec.x] ?? '').slice(0, 12)}</text> : null)}
        </svg>
        <div className="rich-chart-detail" aria-live="polite">{selected !== null ? <><strong>{format(spec.rows[selected]?.[spec.x])}</strong>{series.map(s => <span key={s.key}>{s.label}：{format(spec.rows[selected]?.[s.key])}</span>)}</> : <span>悬停、点击或用 Tab 查看数值</span>}</div>
      </>}
    </>}
  </>
}

export function sandboxDocument(html: string): string {
  const policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><style>html{color-scheme:light}body{font:14px/1.6 system-ui,sans-serif;color:#34402e;background:#fff;padding:16px;margin:0;overflow-wrap:anywhere}button,input,select{font:inherit}button{cursor:pointer}img,svg,canvas{max-width:100%}*{box-sizing:border-box}</style></head><body>${html}</body></html>`
}
function HtmlWidget({ spec }: { spec: Extract<RichSpec, { type: 'html' }> }) {
  const [running, setRunning] = useState(false), [revision, setRevision] = useState(0)
  return <>
    <div className="rich-toolbar"><span>交互组件</span><button onClick={() => setRunning(!running)}>{running ? <Square size={14} /> : <Play size={14} />}{running ? '停止组件' : '运行组件'}</button>{running && <button onClick={() => setRevision(revision + 1)}><RotateCcw size={14} />重置</button>}</div>
    {running ? <iframe key={revision} title={spec.title} className="rich-sandbox" sandbox="allow-scripts" referrerPolicy="no-referrer" allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'" srcDoc={sandboxDocument(spec.html)} style={{ height: spec.height }} /> : <p className="rich-empty">点击运行，使用此组件。重置后交互状态将恢复初始值。</p>}
  </>
}
function Tabs({ spec }: { spec: Extract<RichSpec, { type: 'tabs' }> }) {
  const [active, setActive] = useState(0)
  return <><div className="rich-tablist" role="tablist" aria-label={spec.title}>{spec.tabs.map((tab, i) => <button role="tab" aria-selected={i === active} key={i} onClick={() => setActive(i)}>{tab.label}</button>)}</div><div role="tabpanel" aria-label={spec.tabs[active].label}><WidgetView key={active} spec={spec.tabs[active].content} /></div></>
}
function WidgetView({ spec }: { spec: RichSpec }) {
  return <section className="rich-widget" aria-label={spec.title}><header className="rich-widget-heading"><h3>{spec.title}</h3>{spec.source && <p>来源：{spec.source}</p>}</header>
    {spec.type === 'metrics' && <div className="rich-metrics">{spec.items.length ? spec.items.map((item, i) => <div key={i}><span>{item.label}</span><strong>{format(item.value)}<small>{item.unit}</small></strong>{item.note && <p>{item.note}</p>}</div>) : <p className="rich-empty">暂无指标</p>}</div>}
    {spec.type === 'table' && <DataTable columns={spec.columns} rows={spec.rows} />}
    {spec.type === 'chart' && <Chart spec={spec} />}
    {spec.type === 'image' && <ImagePreview src={contentUrl(spec.url)} alt={spec.caption || spec.title} />}
    {spec.type === 'html' && <HtmlWidget spec={spec} />}
    {spec.type === 'tabs' && <Tabs spec={spec} />}
  </section>
}

export default function RichWidget({ code }: { code: string }) {
  const [source, setSource] = useState(false)
  const parsed = useMemo(() => { try { return { spec: parseRichSpec(code), error: '' } } catch (e) { return { spec: null, error: e instanceof SyntaxError ? '组件内容尚未完整或 JSON 格式有误' : e instanceof Error ? e.message : '无法读取组件' } } }, [code])
  return <div className="rich-artifact">
    {parsed.spec ? <><div className="rich-artifact-actions"><button aria-pressed={source} onClick={() => setSource(!source)}><Code2 size={14} />{source ? '预览' : '查看定义'}</button><button onClick={() => downloadContent(code, 'component.json', 'application/json')}><Download size={14} />下载</button></div>{source ? <CodeBlock code={code} language="json" /> : <WidgetView key={code} spec={parsed.spec} />}</> : <><p className="rich-invalid" role="status">{parsed.error}</p><details><summary>查看原始内容</summary><CodeBlock code={code} language="naumi" /></details></>}
  </div>
}
