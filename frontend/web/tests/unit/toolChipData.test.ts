import { describe, expect, it } from 'vitest'
import { toolChipData } from '../../../web2/src/beautiful/toolChipData'
import type { ToolTimelineStep } from '@naumi/shared/api/activity'

const write = (override: Partial<ToolTimelineStep> = {}): ToolTimelineStep => ({
  id: 'a', kind: 'tool', label: 'file_write', action: '修改文件：E:\\Workspace\\src\\demo.ts',
  state: 'completed', input: '', output: '', ...override,
})
describe('recorded tool chips', () => {
  it('uses actual new-file counts and preserves the full path in details', () => {
    const result = toolChipData([write({ output: '✅ 已创建 E:\\Workspace\\src\\demo.ts (204 行, 999 字符)\n\n```ts\nconst a = 1\n```' })])
    expect(result.steps[0]).toMatchObject({ id: 'a', label: '写入 204 行', chip: 'demo.ts' })
    expect(result.steps[0].detail[0].text).toBe('修改文件：E:\\Workspace\\src\\demo.ts')
    expect(result.diffs[0]).toMatchObject({ add: 204, del: 0 })
  })
  it('counts complete diffs but never treats truncated previews as full counts', () => {
    const output = '✅ 已编辑 demo.ts\n```diff\n--- demo.ts (before)\n+++ demo.ts (after)\n@@ -1 +1,2 @@\n-old\n+new\n+next\n```'
    expect(toolChipData([write({ output })]).diffs[0]).toMatchObject({ add: 2, del: 1 })
    expect(toolChipData([write({ output, outputTruncated: true })]).diffs[0].add).toBeUndefined()
    expect(toolChipData([write({ output: output.replace('+next', '... (80 total diff lines)') })]).diffs[0].add).toBeUndefined()
  })
  it('keeps repeated calls distinct and same-basename files separate', () => {
    const result = toolChipData([write(), write({ id: 'b', action: '修改文件：other/demo.ts' })])
    expect(result.steps.map(row => row.id)).toEqual(['a', 'b'])
    expect(result.diffs).toHaveLength(2)
    expect(result.diffs.every(diff => diff.add === undefined)).toBe(true)
  })
  it('does not invent changes for failures or empty runs; keeps pending output and summaries', () => {
    expect(toolChipData([])).toEqual({ steps: [], diffs: [], diffLines: {} })
    expect(toolChipData([write({ state: 'failed' })]).diffs).toEqual([])
    expect(toolChipData([write({ output: 'Error writing file: PermissionError' })]).diffs).toEqual([])
    const result = toolChipData([write({ state: 'running' }), { id: 'compact', kind: 'reasoning', state: 'completed', turn: 0, label: '已压缩上下文：120 → 30 条消息' }])
    expect(result.steps[0].detail.at(-1)?.text).toBe('等待工具结果…')
    expect(result.steps[1]).toMatchObject({ label: '压缩上下文', chip: '已压缩上下文：120 → 30 条消息' })
  })
})
