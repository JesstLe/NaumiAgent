import type { ExecutionTimelineStep, ToolTimelineStep } from '@naumi/shared/api/activity'
import type { ToolContentBlock, ToolDiff, ToolDiffLine, ToolStep } from './upstream/components/primitives/ToolChips'

export const activityNames = { running: '执行中', completed: '已完成', failed: '失败', cancelled: '已停止', unknown: '状态待确认' }
const basename = (path: string) => path.split(/[\\/]/).filter(Boolean).at(-1) || path
const fileTools = /^(file_write|write_file|write|file_edit)$/
const fileContentTools = /^(file_write|write_file|write|file_edit|file_read|read_file|read)$/
const commandTools = /^(bash_run|shell|run_command|exec_command)$/

const languageFor = (target: string, tool: string) => {
  if (commandTools.test(tool)) return '终端'
  const extension = target.match(/\.([^.\\/]+)$/)?.[1]?.toLowerCase() || ''
  return ({
    js: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx', py: 'python',
    html: 'html', htm: 'html', css: 'css', scss: 'scss', json: 'json',
    md: 'markdown', markdown: 'markdown', yml: 'yaml', yaml: 'yaml',
    sh: 'shell', ps1: 'powershell', sql: 'sql', xml: 'xml', svg: 'svg',
  } as Record<string, string>)[extension] || extension || '文本'
}

function outputPresentation(
  output: string,
  target: string,
  tool: string,
  fallback: string,
): ToolContentBlock[] {
  const text = output.trim()
  if (!text) return [{ kind: 'text', text: fallback }]

  const blocks: ToolContentBlock[] = []
  const fence = /```([^\r\n`]*)\r?\n([\s\S]*?)\r?\n```/g
  let cursor = 0
  for (const match of text.matchAll(fence)) {
    const index = match.index ?? 0
    const prose = text.slice(cursor, index).trim()
    if (prose) blocks.push({ kind: 'text', text: prose })
    const language = match[1].trim().toLowerCase()
    blocks.push({
      kind: language === 'diff' || language === 'patch' ? 'diff' : 'code',
      content: match[2].replace(/\r\n/g, '\n'),
      language: language || languageFor(target, tool),
      label: target || tool,
    })
    cursor = index + match[0].length
  }
  if (blocks.some(block => block.kind !== 'text')) {
    const prose = text.slice(cursor).trim()
    if (prose) blocks.push({ kind: 'text', text: prose })
    return blocks
  }

  const diffStart = text.search(/^(?:diff --git |--- )/m)
  if (diffStart >= 0 && /^@@ /m.test(text.slice(diffStart))) {
    const prefix = text.slice(0, diffStart).trim()
    return [
      ...(prefix ? [{ kind: 'text' as const, text: prefix }] : []),
      { kind: 'diff', content: text.slice(diffStart), language: 'diff', label: target || tool },
    ]
  }
  if (text.includes('```')) return [{ kind: 'text', text }]
  if (/\r?\n/.test(text) && (commandTools.test(tool) || fileContentTools.test(tool))) {
    return [{ kind: 'code', content: text.replace(/\r\n/g, '\n'), language: languageFor(target, tool), label: target || tool }]
  }
  if (/^[\[{]/.test(text)) {
    try {
      JSON.parse(text)
      return [{ kind: 'code', content: text, language: 'json', label: target || tool }]
    } catch { /* Keep malformed JSON as readable text. */ }
  }
  return [{ kind: 'text', text }]
}

/** Only public activity labels and recorded tool results feed this presentation. */
export function toolChipData(timeline: ExecutionTimelineStep[]) {
  const files = new Map<string, { diff: ToolDiff; lines: ToolDiffLine[] }>()
  const steps: ToolStep[] = timeline.map(row => {
    if (row.kind === 'reasoning') {
      const label = /压缩上下文/.test(row.label) ? '压缩上下文' : /^执行计划/.test(row.label) ? '执行计划' : row.turn > 1 ? `第 ${row.turn} 轮` : '任务摘要'
      return { id: row.id, icon: 'think', label, chip: row.label.replace(/^本次任务：/, '').split('\n')[0], state: row.state,
        mono: false, detailMono: false, detail: [{ text: row.label }] }
    }
    const action = row.action || row.label
    // Targets come from the already redacted public action, not arbitrary arguments.
    const separator = action.indexOf('：')
    const operation = separator >= 0 ? action.slice(0, separator) : action
    const target = separator >= 0 ? action.slice(separator + 1) : ''
    const file = /^(读取文件|修改文件|写入文件|编辑文件)/.test(operation)
    const icon = fileTools.test(row.label) ? 'write' : /read|读取|search|glob|grep/.test(row.label) ? 'read' : 'run'
    const created = row.state === 'completed' && fileTools.test(row.label) ? row.output.match(/^✅ 已创建 .+ \((\d+) 行, \d+ 字符\)/) : null
    const written = row.state === 'completed' && fileTools.test(row.label) ? row.output.match(/^✅ 已(?:创建|覆写) .+ \((\d+) 行, \d+ 字符\)/) : null
    const label = written ? `写入 ${written[1]} 行` : operation === '在工作目录执行命令' ? '执行命令' : operation
    const content = outputPresentation(
      row.output,
      target,
      row.label,
      row.state === 'running' ? '等待工具结果…' : row.outputRecorded ? '工具未返回文本内容' : '旧记录未保存工具输出',
    )
    if (fileTools.test(row.label) && target && row.state === 'completed' && !/^(Error|错误|失败)/i.test(row.output)) {
      const record = fileChange(row, target, created ? Number(created[1]) : undefined)
      const prior = files.get(target)
      if (prior) {
        record.diff.add = prior.diff.add !== undefined && record.diff.add !== undefined ? prior.diff.add + record.diff.add : undefined
        record.diff.del = prior.diff.del !== undefined && record.diff.del !== undefined ? prior.diff.del + record.diff.del : undefined
        record.lines = [...prior.lines, { text: '下一次修改', tone: 'ctx' }, ...record.lines]
      }
      files.set(target, record)
    }
    return {
      id: row.id, ariaLabel: `${row.label} ${activityNames[row.state]}`, state: row.state,
      input: row.input,
      icon, label: label.replace(/^调用工具 /, ''), chip: target ? file ? basename(target) : target : row.label,
      mono: Boolean(target), detailMono: true,
      detail: [
        { text: action }, { text: `状态：${activityNames[row.state]}` },
      ],
      content: [
        ...content,
        ...(row.outputTruncated ? [{ kind: 'text' as const, text: '当前记录仅包含工具返回的输出预览。' }] : []),
      ],
    }
  })
  return { steps, diffs: [...files.values()].map(record => record.diff), diffLines: Object.fromEntries([...files].map(([path, record]) => [path, record.lines])) }
}

function fileChange(row: ToolTimelineStep, path: string, created?: number) {
  const diff: ToolDiff = { file: path, label: basename(path) }
  const lines: ToolDiffLine[] = [{ text: '本次执行中的累计修改；内容来自工具记录', tone: 'ctx' }]
  const fenced = row.output.match(/```diff\r?\n([\s\S]*?)\r?\n```/)
  if (created !== undefined) {
    diff.add = created
    diff.del = 0
    lines.push({ text: `新建文件，共 ${created} 行`, tone: 'ctx' })
    const preview = row.output.match(/```[^\n]*\n([\s\S]*?)\n```/)
    if (preview) lines.push(...preview[1].split('\n').map(text => ({ text, tone: 'ctx' as const })))
  } else if (fenced) {
    const recorded = fenced[1].split(/\r?\n/)
    const firstHunk = recorded.findIndex(text => text.startsWith('@@ '))
    const changes = recorded.slice(Math.max(0, firstHunk)).filter(Boolean)
    lines.push(...changes.map(text => ({ text: /^[+ -]/.test(text) ? text.slice(1) : text, tone: text.startsWith('+') ? 'add' as const : text.startsWith('-') ? 'del' as const : 'ctx' as const })))
    if (!row.outputTruncated && !/total diff lines|more lines|已截断/.test(fenced[1]) && /^@@ /m.test(fenced[1])) {
      diff.add = changes.filter(text => text.startsWith('+')).length
      diff.del = changes.filter(text => text.startsWith('-')).length
    } else lines.push({ text: '仅保存部分差异，未统计完整增删行数', tone: 'ctx' })
  } else {
    lines.push({ text: row.output.split('\n')[0] || '未保存文件差异', tone: 'ctx' })
  }
  return { diff, lines }
}
