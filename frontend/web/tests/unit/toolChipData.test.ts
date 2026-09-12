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
    expect(result.steps[0].content?.find(block => block.kind === 'code')).toMatchObject({ kind: 'code', language: 'ts', content: 'const a = 1' })
    expect(result.steps[0].detail.map(line => line.text).join('\n')).not.toContain('```ts')
    expect(result.diffs[0]).toMatchObject({ add: 204, del: 0 })
  })
  it('counts complete diffs but never treats truncated previews as full counts', () => {
    const output = '✅ 已编辑 demo.ts\n```diff\n--- demo.ts (before)\n+++ demo.ts (after)\n@@ -1 +1,2 @@\n-old\n+new\n+next\n```'
    expect(toolChipData([write({ output })]).diffs[0]).toMatchObject({ add: 2, del: 1 })
    expect(toolChipData([write({ output })]).steps[0].content?.find(block => block.kind === 'diff')).toMatchObject({
      kind: 'diff',
      content: expect.stringContaining('@@ -1 +1,2 @@'),
    })
    expect(toolChipData([write({ output, outputTruncated: true })]).diffs[0].add).toBeUndefined()
    expect(toolChipData([write({ output: output.replace('+next', '... (80 total diff lines)') })]).diffs[0].add).toBeUndefined()
  })
  it('renders multiline command and unfenced diff output as structured blocks', () => {
    const command = write({ label: 'bash_run', action: '在工作目录执行命令：pnpm test', output: 'PASS unit\n100 tests passed' })
    expect(toolChipData([command]).steps[0].content?.[0]).toMatchObject({
      kind: 'code', language: '终端', content: 'PASS unit\n100 tests passed',
    })
    const raw = write({ output: '✅ 已编辑 demo.ts\n--- before\n+++ after\n@@ -1 +1 @@\n-old\n+new' })
    expect(toolChipData([raw]).steps[0]).toMatchObject({
      content: [
        { kind: 'text', text: '✅ 已编辑 demo.ts' },
        { kind: 'diff', content: expect.stringContaining('-old\n+new') },
      ],
    })
  })
  it('keeps prose readable while rendering multiple fenced blocks independently', () => {
    const output = '先看配置：\n```json\n{"ok":true}\n```\n再看命令：\n```sh\npnpm test\n```\n完成。'
    const step = toolChipData([write({ label: 'read', action: '读取文件：demo.json', output })]).steps[0]
    expect(step.detail.map(line => line.text)).toEqual(['读取文件：demo.json', '状态：已完成'])
    expect(step.content).toMatchObject([
      { kind: 'text', text: '先看配置：' },
      { kind: 'code', language: 'json', content: '{"ok":true}' },
      { kind: 'text', text: '再看命令：' },
      { kind: 'code', language: 'sh', content: 'pnpm test' },
      { kind: 'text', text: '完成。' },
    ])
  })
  it('does not turn ordinary multiline prose or an incomplete fence into code', () => {
    const prose = write({ label: 'browser_goto', action: '访问页面：https://example.com', output: '页面已打开\n标题：示例页面' })
    expect(toolChipData([prose]).steps[0].content).toEqual([{ kind: 'text', text: '页面已打开\n标题：示例页面' }])
    const incomplete = write({ label: 'read', action: '读取文件：demo.ts', output: '```ts\nconst broken = true' })
    expect(toolChipData([incomplete]).steps[0].content).toEqual([{ kind: 'text', text: '```ts\nconst broken = true' }])
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
    expect(result.steps[0].content?.[0]).toEqual({ kind: 'text', text: '等待工具结果…' })
    expect(result.steps[1]).toMatchObject({ label: '压缩上下文', chip: '已压缩上下文：120 → 30 条消息' })
  })
})
