import { describe, expect, it } from 'vitest'
import { parseUnifiedDiff, splitDiffLines } from '../../src/api/diff'

describe('Git patch presentation', () => {
  it('keeps metadata out of counts and resets numbers at each hunk', () => {
    const rows = parseUnifiedDiff('--- a/x\n+++ b/x\n@@ -4,2 +4,3 @@\n old\n-gone\n+new\n+extra\n@@ -20 +21 @@\n-end\n+ending\n\\ No newline at end of file\n')
    expect(rows[0].kind).toBe('meta')
    expect(rows[1].kind).toBe('meta')
    expect(rows[4]).toMatchObject({ kind: 'removed', oldLine: 5, newLine: null })
    expect(rows[6]).toMatchObject({ kind: 'added', oldLine: null, newLine: 6 })
    expect(rows[9]).toMatchObject({ kind: 'added', newLine: 21 })
    expect(rows[10].kind).toBe('meta')
    const split = splitDiffLines(rows)
    expect(split[4].left?.text).toBe('gone')
    expect(split[4].right?.text).toBe('new')
    expect(split[5].left).toBeNull()
    expect(split[5].right?.text).toBe('extra')
  })
  it('supports new files, CRLF and empty/binary patches', () => {
    expect(parseUnifiedDiff('')).toEqual([])
    expect(parseUnifiedDiff('Binary files differ')[0].kind).toBe('meta')
    expect(parseUnifiedDiff('@@ -0,0 +1 @@\r\n+中文\r\n')[1]).toMatchObject({ oldLine: null, newLine: 1, text: '中文' })
  })
})
