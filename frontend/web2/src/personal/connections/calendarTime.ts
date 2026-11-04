function parts(timestamp: number, zone: string) {
  const values = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(timestamp)
  const get = (name: string) => values.find(value => value.type === name)?.value || ''
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}:${get('second')}`
}
export function calendarWallTime(instant: string, zone: string): string {
  const timestamp = Date.parse(instant)
  return Number.isFinite(timestamp) ? parts(timestamp, zone).slice(0, 16) : ''
}
export function calendarInstant(local: string, zone: string, fold: 0 | 1 = 0): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local)) throw new Error('请填写完整日期和时间')
  const wall = local + ':00', naive = Date.parse(wall + 'Z')
  if (!Number.isFinite(naive) || new Date(naive).toISOString().slice(0, 19) !== wall) throw new Error('日期或时间不正确')
  const offsets = new Set<number>()
  for (let hour = -24; hour <= 24; hour += 6) {
    const instant = naive + hour * 3600000
    offsets.add(Date.parse(parts(instant, zone) + 'Z') - instant)
  }
  const matches = [...offsets].map(offset => naive - offset).filter(instant => parts(instant, zone) === wall).sort((a, b) => a - b)
  if (!matches.length) throw new Error('该本地时间处于夏令时跳时区间，请重新选择')
  return new Date(matches[Math.min(fold, matches.length - 1)]).toISOString()
}
