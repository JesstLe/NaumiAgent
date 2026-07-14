// Shared date formatting utilities.
export function formatDate(date: string | Date): string {
  return new Date(date).toLocaleString('zh-CN')
}

const SECOND = 1000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function formatRelativeTime(date: string | Date): string {
  const value = new Date(date).getTime()
  const now = Date.now()
  const diff = now - value

  if (Number.isNaN(diff)) return ''
  if (diff < MINUTE) return '刚刚'
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`
  return `${Math.floor(diff / DAY)} 天前`
}
