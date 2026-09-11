export interface DiffLine {
  kind: 'context' | 'added' | 'removed' | 'hunk' | 'meta'
  text: string
  oldLine: number | null
  newLine: number | null
}

export function parseUnifiedDiff(patch: string): DiffLine[] {
  let oldLine = 0
  let newLine = 0
  let inHunk = false
  const lines = patch.replace(/\r\n/g, '\n').split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines.map(text => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text)
    if (hunk) {
      oldLine = Number(hunk[1]); newLine = Number(hunk[2]); inHunk = true
      return { kind: 'hunk', text, oldLine: null, newLine: null }
    }
    if (text.startsWith('diff --git ')) inHunk = false
    if (!inHunk || text.startsWith('\\')) return { kind: 'meta', text, oldLine: null, newLine: null }
    if (text.startsWith('+')) return { kind: 'added', text: text.slice(1), oldLine: null, newLine: newLine++ }
    if (text.startsWith('-')) return { kind: 'removed', text: text.slice(1), oldLine: oldLine++, newLine: null }
    if (text.startsWith(' ')) return { kind: 'context', text: text.slice(1), oldLine: oldLine++, newLine: newLine++ }
    return { kind: 'meta', text, oldLine: null, newLine: null }
  })
}
export interface SplitDiffLine { left: DiffLine | null; right: DiffLine | null; header?: DiffLine }
export function splitDiffLines(lines: DiffLine[]): SplitDiffLine[] {
  const result: SplitDiffLine[] = []
  let removed: DiffLine[] = []
  let added: DiffLine[] = []
  const flush = () => {
    for (let index = 0; index < Math.max(removed.length, added.length); index++) result.push({ left: removed[index] ?? null, right: added[index] ?? null })
    removed = []; added = []
  }
  for (const line of lines) {
    if (line.kind === 'removed') { if (added.length) flush(); removed.push(line) }
    else if (line.kind === 'added') added.push(line)
    else {
      flush()
      result.push(line.kind === 'context' ? { left: line, right: line } : { left: null, right: null, header: line })
    }
  }
  flush()
  return result
}
