import type { TodayOperation, TodayQuery } from '@naumi/shared/api/personalToday'

export const todayRequestId = () => crypto.randomUUID().replaceAll('-', '')
export const todayPendingKey = (namespace: string) => `naumi:today:request:${encodeURIComponent(namespace)}`
export interface TodayPending { version: 1; request_id: string; operation: TodayOperation; workspace_key: string; day_id?: string; metadata_id?: string }
export function parseTodayPending(raw: string): TodayPending {
  const value = JSON.parse(raw) as TodayPending
  const allowed = ['version', 'request_id', 'operation', 'workspace_key', 'day_id', 'metadata_id']
  if (!value || typeof value !== 'object' || Object.keys(value).some(key => !allowed.includes(key)) || value.version !== 1
    || !/^[a-f0-9]{32}$/.test(value.request_id) || !/^[a-f0-9]{64}$/.test(value.workspace_key)
    || !['set-metadata', 'place', 'reorder', 'remove', 'rebase', 'check', 'move'].includes(value.operation)
    || value.day_id !== undefined && !/^td_[a-f0-9]{32}$/.test(value.day_id)
    || value.metadata_id !== undefined && !/^tm_[a-f0-9]{32}$/.test(value.metadata_id))
    throw new Error('原请求身份待核对')
  return value
}
export function todayDate(now = new Date(), timezone?: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now)
  const get = (part: string) => parts.find(value => value.type === part)?.value
  return `${get('year')}-${get('month')}-${get('day')}`
}
export function readTodayLocation(): TodayQuery {
  const query = new URLSearchParams(location.search)
  let timezone = query.get('today_timezone') || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'
  try { new Intl.DateTimeFormat('zh-CN', { timeZone: timezone }).format() } catch { timezone = 'Asia/Shanghai' }
  const date = query.get('today_date') || todayDate(new Date(), timezone)
  return { date: /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : todayDate(), timezone, ...(query.get('today_planner') ? { planner_id: query.get('today_planner')! } : {}) }
}
export function writeTodayLocation(query: TodayQuery) {
  const url = new URL(location.href)
  url.searchParams.set('today', '1'); url.searchParams.set('today_date', query.date); url.searchParams.set('today_timezone', query.timezone)
  if (query.planner_id) url.searchParams.set('today_planner', query.planner_id)
  else url.searchParams.delete('today_planner')
  history.replaceState({}, '', url)
  window.dispatchEvent(new Event('naumi:today-location'))
}
