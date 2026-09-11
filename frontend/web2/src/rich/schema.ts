export type Cell = string | number | boolean | null
export type Column = { key: string; label: string }
export type Row = Record<string, Cell>
export type RichSpec =
  | { type: 'metrics'; title: string; source?: string; items: { label: string; value: string | number; unit?: string; note?: string }[] }
  | { type: 'table'; title: string; source?: string; columns: Column[]; rows: Row[] }
  | { type: 'chart'; title: string; source?: string; x: string; series: { key: string; label: string }[]; rows: Row[]; style: 'bar' | 'line' | 'scatter' }
  | { type: 'image'; title: string; source?: string; url: string; caption?: string }
  | { type: 'html'; title: string; source?: string; html: string; height: number }
  | { type: 'tabs'; title: string; source?: string; tabs: { label: string; content: RichSpec }[] }

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('组件应为 JSON 对象')
  return value as Record<string, unknown>
}
function text(value: unknown, name: string, max = 500): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max) throw Error(`${name}需要 1–${max} 个字符`)
  return value
}
function list(value: unknown, max: number, name: string): unknown[] {
  if (!Array.isArray(value) || value.length > max) throw Error(`${name}最多支持 ${max} 项`)
  return value
}
function columns(value: unknown): Column[] {
  const result = list(value, 20, '字段').map(v => { const item = object(v); return { key: text(item.key, '字段名', 80), label: text(item.label, '字段标题', 80) } })
  if (!result.length || new Set(result.map(c => c.key)).size !== result.length) throw Error('字段不能为空或重复')
  return result
}
function rows(value: unknown): Row[] {
  return list(value, 2000, '数据行').map(v => {
    const row = object(v)
    if (Object.keys(row).length > 30) throw Error('每行最多支持 30 个字段')
    for (const cell of Object.values(row)) {
      if (cell !== null && typeof cell !== 'boolean' && !(typeof cell === 'string' && cell.length <= 2000) && !(typeof cell === 'number' && Number.isFinite(cell))) throw Error('单元格仅支持有限数值、文本、布尔值或 null')
    }
    return row as Row
  })
}
function spec(value: unknown, depth = 0): RichSpec {
  if (depth > 2) throw Error('组件嵌套不能超过三层')
  const v = object(value)
  const common = { title: text(v.title, '标题', 120), source: v.source === undefined ? undefined : text(v.source, '数据来源') }
  switch (v.type) {
    case 'metrics': return { ...common, type: v.type, items: list(v.items, 12, '指标').map(value => {
      const item = object(value)
      if (typeof item.value !== 'string' && !(typeof item.value === 'number' && Number.isFinite(item.value))) throw Error('指标值必须是文本或有限数值')
      return { label: text(item.label, '指标名称', 80), value: typeof item.value === 'string' ? text(item.value, '指标值', 120) : item.value, unit: item.unit === undefined ? undefined : text(item.unit, '单位', 30), note: item.note === undefined ? undefined : text(item.note, '指标说明', 300) }
    }) }
    case 'table': return { ...common, type: v.type, columns: columns(v.columns), rows: rows(v.rows) }
    case 'chart': {
      const data = rows(v.rows), series = columns(v.series), x = text(v.x, '横轴', 80)
      if (!['bar', 'line', 'scatter', undefined].includes(v.style as string | undefined)) throw Error('图表支持 bar、line、scatter')
      if (series.length > 6 || data.length > 300) throw Error('图表最多支持 6 个系列、300 行数据，请先聚合')
      for (const row of data) for (const s of series) if (row[s.key] !== null && row[s.key] !== undefined && typeof row[s.key] !== 'number') throw Error('图表系列必须为数值或 null')
      return { ...common, type: v.type, x, series, rows: data, style: (v.style || 'bar') as 'bar' | 'line' | 'scatter' }
    }
    case 'image': return { ...common, type: v.type, url: text(v.url, '图片地址', 2048), caption: v.caption === undefined ? undefined : text(v.caption, '图片说明') }
    case 'html': return { ...common, type: v.type, html: text(v.html, 'HTML', 100000), height: typeof v.height === 'number' && Number.isFinite(v.height) ? Math.min(900, Math.max(240, v.height)) : 420 }
    case 'tabs': {
      const tabs = list(v.tabs, 8, '标签页').map(tab => { const t = object(tab); return { label: text(t.label, '标签', 50), content: spec(t.content, depth + 1) } })
      if (!tabs.length) throw Error('标签页不能为空')
      return { ...common, type: v.type, tabs }
    }
    default: throw Error('不支持的组件类型；可使用 metrics、table、chart、image、html、tabs')
  }
}
export function parseRichSpec(code: string): RichSpec {
  if (code.length > 300000) throw Error('组件超过 300 KB，请返回数据文件或聚合结果')
  const value = object(JSON.parse(code))
  if (value.version !== 1) throw Error('组件 version 必须为 1')
  return spec(value)
}
export function toCsv(columns: Column[], rows: Row[]): string {
  const quote = (v: Cell | undefined) => {
    let value = String(v ?? '')
    if (typeof v === 'string' && /^[\s]*[=+@-]/.test(value)) value = `'${value}`
    return `"${value.replaceAll('"', '""')}"`
  }
  return '\uFEFF' + [columns.map(c => quote(c.label)), ...rows.map(row => columns.map(c => quote(row[c.key])))].map(row => row.join(',')).join('\r\n')
}
