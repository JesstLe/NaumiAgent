export function selectionPrompt(action: 'explain' | 'improve' | 'shorten', text: string, draft: string): string {
  const instruction = { explain: '请解释以下内容：', improve: '请改写以下内容，使表达更清晰：', shorten: '请精简以下内容，保留关键事实：' }[action]
  const quote = text.trim().split('\n').map(line => `> ${line}`).join('\n')
  return `${draft.trim() ? draft.trimEnd() + '\n\n' : ''}${instruction}\n\n${quote}`
}
