import { describe, expect, it } from 'vitest'
import { dependencyGraph } from '../../src/api/dependencies'
import { distribution } from '../../src/api/distribution'
import type { Todo } from '../../src/api/WorkbenchRuntimeClient'
const todo = (id: string, blocked_by: string[] = []): Todo => ({ id, blocked_by, subject: id, description: '', status: 'pending', active_form: null, updated_at: '' })
describe('dependency evidence', () => {
  it('orders prerequisites and deduplicates links', () => {
    const graph = dependencyGraph([todo('c', ['b', 'b']), todo('b', ['a']), todo('a')])
    expect(graph.ordered.map(node => node.id)).toEqual(['a', 'b', 'c'])
    expect(graph.edges).toHaveLength(2)
    expect(graph.warnings).toEqual([])
  })
  it('keeps cycles and reports unavailable prerequisites', () => {
    const graph = dependencyGraph([todo('a', ['b']), todo('b', ['a']), todo('c', ['missing'])])
    expect(graph.ordered).toHaveLength(3)
    expect(graph.warnings).toHaveLength(2)
    expect(graph.edges).toEqual([{ from: 'b', to: 'a' }, { from: 'a', to: 'b' }])
    expect(dependencyGraph([])).toEqual({ ordered: [], edges: [], warnings: [] })
  })
  it('handles self dependencies', () => expect(dependencyGraph([todo('a', ['a'])]).warnings[0]).toContain('循环'))
})
describe('observed status distribution', () => {
  it('retains unknown states and rounds without losing percentage points', () => {
    const rows = distribution(['a', 'b', 'unknown'], { a: '正常', b: '异常' })
    expect(rows.map(row => row.count)).toEqual([1, 1, 1])
    expect(rows.map(row => row.percent)).toEqual([33.4, 33.3, 33.3])
    expect(rows[2].label).toBe('unknown')
  })
  it('does not fabricate observations for empty data', () => expect(distribution([], {})).toEqual([]))
})
