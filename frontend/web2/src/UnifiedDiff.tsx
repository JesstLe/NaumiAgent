import { useMemo } from 'react'
import { parseUnifiedDiff, splitDiffLines, type DiffLine } from '@naumi/shared/api/diff'

function DiffCell({ line, side }: { line: DiffLine | null; side: 'left' | 'right' }) {
  return <div className={`w2-split-cell ${line?.kind ?? 'empty'}`}>
    <span className="w2-line-number">{side === 'left' ? line?.oldLine : line?.newLine}</span>
    <code>{line?.text || ' '}</code>
  </div>
}

export function UnifiedDiff({
  patch,
  label,
  split = false,
  className = '',
}: {
  patch: string
  label: string
  split?: boolean
  className?: string
}) {
  const lines = useMemo(() => parseUnifiedDiff(patch), [patch])
  const splitLines = useMemo(() => split ? splitDiffLines(lines) : [], [lines, split])
  return <div className={`w2-code-diff ${split ? 'is-split' : ''}${className ? ` ${className}` : ''}`} tabIndex={0} aria-label={label}>
    {split
      ? splitLines.map((row, index) => row.header
        ? <div key={index} className={`w2-diff-line ${row.header.kind}`}>{row.header.text}</div>
        : <div className="w2-split-row" key={index}><DiffCell line={row.left} side="left" /><DiffCell line={row.right} side="right" /></div>)
      : lines.map((line, index) => <div className={`w2-diff-line ${line.kind}`} key={index}>
        <span className="w2-line-number">{line.oldLine}</span>
        <span className="w2-line-number">{line.newLine}</span>
        <span className="w2-line-sign">{line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '}</span>
        <code>{line.text || ' '}</code>
      </div>)}
  </div>
}
