import { useEffect, useState, type FormEvent } from 'react'
import { Check, Plus, Trash2 } from 'lucide-react'
import type { NotificationPreferenceInput, NotificationPreferencesView, NotificationQuietWindow } from '@naumi/shared/api/personalNotifications'

const weekdays = ['一', '二', '三', '四', '五', '六', '日']
export function notificationPreferenceInput(value: NotificationPreferencesView): NotificationPreferenceInput {
  const prefs = value.preferences
  return { enabled: prefs.enabled, timezone: prefs.timezone, quiet_hours: prefs.quiet_hours.map(window => ({ ...window, weekdays: [...window.weekdays] })), muted_actor_ids: [...prefs.muted_actor_ids], debounce_seconds: prefs.debounce_seconds, max_delay_seconds: prefs.max_delay_seconds, bypass_kinds: [...prefs.bypass_kinds], include_existing: prefs.include_existing }
}
export function NotificationSettings({ preferences, actors, locked, onSave }: {
  preferences: NotificationPreferencesView; actors: { id: string; name: string }[]; locked: boolean
  onSave: (input: NotificationPreferenceInput, expectedRevision: number) => Promise<boolean>
}) {
  const [base, setBase] = useState(preferences), [draft, setDraft] = useState(() => notificationPreferenceInput(preferences))
  const [error, setError] = useState(''), [saved, setSaved] = useState(false)
  const changed = base.revision !== preferences.revision
  useEffect(() => {
    if (saved && preferences.revision === base.revision + 1) { setBase(preferences); setDraft(notificationPreferenceInput(preferences)) }
  }, [saved, preferences, base.revision])
  const set = <K extends keyof NotificationPreferenceInput>(key: K, value: NotificationPreferenceInput[K]) => { setSaved(false); setDraft(previous => ({ ...previous, [key]: value })) }
  const updateWindow = (index: number, patch: Partial<NotificationQuietWindow>) => set('quiet_hours', draft.quiet_hours.map((window, position) => position === index ? { ...window, ...patch } : window))
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setSaved(false)
    try {
      try { new Intl.DateTimeFormat('zh-CN', { timeZone: draft.timezone }) } catch { throw new Error('请输入有效的 IANA 时区') }
      if (!Number.isSafeInteger(draft.debounce_seconds) || draft.debounce_seconds < 0 || draft.debounce_seconds > 300) throw new Error('合并等待时间应为 0–300 秒')
      if (!Number.isSafeInteger(draft.max_delay_seconds) || draft.max_delay_seconds < 1 || draft.max_delay_seconds > 3600 || draft.max_delay_seconds < draft.debounce_seconds) throw new Error('最长等待时间应为 1–3600 秒，且不短于合并等待时间')
      if (draft.quiet_hours.some(window => !window.weekdays.length || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.end) || window.start === window.end && !window.full_day)) throw new Error('请完整选择勿扰日期与时间；相同起止时间需选择全天')
      if (await onSave(draft, base.revision)) setSaved(true)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '通知设置尚未保存') }
  }
  const allActors = [...actors, ...draft.muted_actor_ids.filter(id => !actors.some(actor => actor.id === id)).map(id => ({ id, name: id }))]
  return <form className="pn-settings" aria-label="通知设置" onSubmit={event => void submit(event)}>
    {error && <div className="pn-error" role="alert">{error}</div>}
    {changed && <div className="pn-recovery" role="status"><span>设置已有新版本，当前编辑已保留</span><button type="button" disabled={locked} onClick={() => { setBase(preferences); setDraft(notificationPreferenceInput(preferences)); setError(''); setSaved(false) }}>载入新版本</button></div>}
    <fieldset disabled={locked}>
      <label className="pn-toggle"><span>主动提醒</span><input type="checkbox" aria-label="主动提醒" checked={draft.enabled} onChange={event => set('enabled', event.target.checked)} /><span className="pn-switch" aria-hidden="true" /></label>
      <label>时区<input required aria-label="通知时区" value={draft.timezone} maxLength={128} list="pn-timezones" onChange={event => set('timezone', event.target.value)} /></label>
      <datalist id="pn-timezones">{['Asia/Shanghai', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Australia/Sydney', 'UTC'].map(zone => <option key={zone} value={zone} />)}</datalist>
      <section className="pn-settings-section"><header><h3>勿扰时段</h3><button type="button" disabled={draft.quiet_hours.length >= 14} onClick={() => set('quiet_hours', [...draft.quiet_hours, { weekdays: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '07:00', full_day: false }])}><Plus size={14} />添加</button></header>
        {draft.quiet_hours.map((window, index) => <div className="pn-quiet-window" key={index}>
          <div className="pn-weekdays" role="group" aria-label={`勿扰时段 ${index + 1} 日期`}>{weekdays.map((label, day) => <button type="button" key={day} aria-label={`时段 ${index + 1} 周${label}`} aria-pressed={window.weekdays.includes(day)} onClick={() => updateWindow(index, { weekdays: window.weekdays.includes(day) ? window.weekdays.filter(value => value !== day) : [...window.weekdays, day].sort((a, b) => a - b) })}>{label}</button>)}</div>
          <div className="pn-window-time"><input type="time" required aria-label={`时段 ${index + 1} 开始`} value={window.start} onChange={event => updateWindow(index, { start: event.target.value, ...(window.full_day ? { end: event.target.value } : {}) })} /><span>—</span><input type="time" required disabled={window.full_day} aria-label={`时段 ${index + 1} 结束`} value={window.end} onChange={event => updateWindow(index, { end: event.target.value })} /><label className="pn-check"><input type="checkbox" aria-label={`时段 ${index + 1} 全天`} checked={window.full_day} onChange={event => updateWindow(index, { full_day: event.target.checked, ...(event.target.checked ? { end: window.start } : {}) })} />全天</label><button type="button" aria-label={`删除勿扰时段 ${index + 1}`} onClick={() => set('quiet_hours', draft.quiet_hours.filter((_, position) => position !== index))}><Trash2 size={15} /></button></div>
        </div>)}
        {!draft.quiet_hours.length && <div className="pn-setting-empty">未设置</div>}
      </section>
      <section className="pn-settings-section"><header><h3>合并提醒</h3></header><div className="pn-form-grid"><label>合并等待（秒）<input type="number" required min={0} max={300} step={1} aria-label="合并等待秒数" value={draft.debounce_seconds} onChange={event => set('debounce_seconds', Number(event.target.value))} /></label><label>最长等待（秒）<input type="number" required min={1} max={3600} step={1} aria-label="最长等待秒数" value={draft.max_delay_seconds} onChange={event => set('max_delay_seconds', Number(event.target.value))} /></label></div></section>
      <section className="pn-settings-section"><header><h3>允许绕过勿扰</h3></header>{(['item_failure', 'calendar_approval'] as const).map(kind => <label className="pn-check" key={kind}><input type="checkbox" checked={draft.bypass_kinds.includes(kind)} onChange={event => set('bypass_kinds', event.target.checked ? [...draft.bypass_kinds, kind] : draft.bypass_kinds.filter(value => value !== kind))} />{kind === 'item_failure' ? '执行失败' : '日历待审批'}</label>)}</section>
      <section className="pn-settings-section"><header><h3>静音 Agent</h3></header><div className="pn-muted-actors">{allActors.map(actor => <label className="pn-check" key={actor.id}><input type="checkbox" checked={draft.muted_actor_ids.includes(actor.id)} onChange={event => set('muted_actor_ids', event.target.checked ? [...draft.muted_actor_ids, actor.id] : draft.muted_actor_ids.filter(id => id !== actor.id))} /><span>{actor.name}</span></label>)}</div></section>
      <label className="pn-check"><input type="checkbox" checked={draft.include_existing} onChange={event => set('include_existing', event.target.checked)} />包含现有结果与待审批动作</label>
    </fieldset>
    <footer><span>{saved && <><Check size={14} />已保存</>}</span><button type="submit" className="pn-primary" disabled={locked || changed}>保存设置</button></footer>
  </form>
}
