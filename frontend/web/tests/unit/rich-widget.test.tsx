import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import RichWidget, { sandboxDocument } from '../../../web2/src/rich/RichWidget'
import { parseRichSpec, toCsv } from '../../../web2/src/rich/schema'

const table = { version: 1, type: 'table', title: '订单', columns: [{ key: 'name', label: '商品' }, { key: 'sales', label: '销售额' }], rows: [{ name: '苹果', sales: 12 }, { name: '香蕉', sales: -5 }, { name: '空值', sales: null }] }

describe('rich interactive components', () => {
  it('sorts numbers, searches and computes statistics from filtered rows', () => {
    render(<RichWidget code={JSON.stringify(table)} />)
    fireEvent.click(screen.getByRole('button', { name: /销售额/ }))
    expect(within(screen.getAllByRole('row')[1]).getByText('香蕉')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: '搜索表格' }), { target: { value: '苹果' } })
    expect(screen.queryByText('香蕉')).toBeNull()
    expect(screen.getByText('合计 12')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: '搜索表格' }), { target: { value: '不存在' } })
    expect(screen.getByText('没有匹配的数据')).toBeInTheDocument()
  })
  it('paginates and allows empty datasets', () => {
    const { rerender } = render(<RichWidget code={JSON.stringify({ ...table, rows: Array.from({ length: 21 }, (_, i) => ({ name: `产品${i}`, sales: i })) })} />)
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('产品20')).toBeInTheDocument()
    rerender(<RichWidget code={JSON.stringify({ ...table, rows: [] })} />)
    expect(screen.getByText('暂无数据')).toBeInTheDocument()
  })
  it('handles negative chart values, hidden series, nulls and a table view', () => {
    render(<RichWidget code={JSON.stringify({ version: 1, type: 'chart', title: '趋势', x: 'name', series: [{ key: 'sales', label: '销售额' }], rows: table.rows })} />)
    expect(screen.getByRole('img', { name: '趋势' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '销售额', exact: true }))
    expect(screen.getByText('选择上方系列以显示图表')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '数据', exact: true }))
    expect(screen.getByRole('table')).toHaveTextContent('-5')
  })
  it('requires an explicit start and isolates HTML from host privileges', () => {
    const { container } = render(<RichWidget code={JSON.stringify({ version: 1, type: 'html', title: '计数器', html: '<button>+1</button>' })} />)
    expect(container.querySelector('iframe')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '运行组件' }))
    const frame = screen.getByTitle('计数器')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
    expect(frame.getAttribute('srcdoc')).toContain("connect-src 'none'")
    expect(sandboxDocument('hello')).toContain("form-action 'none'")
    fireEvent.click(screen.getByRole('button', { name: '停止组件' }))
    expect(container.querySelector('iframe')).toBeNull()
  })
  it('validates version, malformed JSON, size, depth and finite numbers', () => {
    for (const value of ['{', JSON.stringify({ ...table, version: 2 }), JSON.stringify({ ...table, rows: Array(2001).fill({}) }), JSON.stringify({ ...table, type: 'unknown' }), '{"version":1,"type":"metrics","title":"t","items":[{"label":"x","value":1e999}]}']) expect(() => parseRichSpec(value)).toThrow()
    const { container } = render(<RichWidget code="{" />)
    expect(screen.getByRole('status')).toHaveTextContent('尚未完整')
    expect(container.querySelector('iframe')).toBeNull()
    const nested = { type: 'tabs', title: 'a', tabs: [{ label: 'a', content: { type: 'tabs', title: 'b', tabs: [{ label: 'b', content: { type: 'tabs', title: 'c', tabs: [{ label: 'c', content: table }] } }] } }] }
    expect(() => parseRichSpec(JSON.stringify({ ...nested, version: 1 }))).toThrow(/嵌套/)
  })
  it('escapes CSV and neutralizes spreadsheet formulas', () => {
    const csv = toCsv([{ key: 'name', label: '商品' }], [{ name: '=cmd()' }, { name: 'a,"b"' }])
    expect(csv).toContain('"\'=cmd()"')
    expect(csv).toContain('"a,""b"""')
  })
})
