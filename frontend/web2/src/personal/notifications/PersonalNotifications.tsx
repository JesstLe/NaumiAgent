import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Bell, BellOff, CalendarDays, Check, CheckCheck, ChevronRight, CircleAlert, Inbox, Loader2, Moon, RefreshCw, Settings2, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { NotificationBatch, PersonalNotificationEntry } from '@naumi/shared/api/personalNotifications'
import { NotificationSettings } from './NotificationSettings'
import { usePersonalNotifications, type PersonalNotificationsApi, type PersonalNotificationsState } from './usePersonalNotifications'
import './personal-notifications.css'

const kindNames = { item_result: '执行结果', item_failure: '执行失败', calendar_approval: '日历待审批' }
const when = (value: string) => new Date(value).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
function KindIcon({ kind }: { kind: PersonalNotificationEntry['kind'] }) { return kind === 'calendar_approval' ? <CalendarDays size={17} /> : kind === 'item_failure' ? <CircleAlert size={17} /> : <Inbox size={17} /> }

function ActiveToast({ batch, state, onOpen }: { batch: NotificationBatch; state: PersonalNotificationsState; onOpen: () => void }) {
  const ref = useRef<HTMLDivElement>(null), members = useRef<HTMLDivElement>(null), reported = useRef(false), seen = useRef(new Set<string>())
  useEffect(() => {
    let frame = 0
    const report = () => {
      if (reported.current || document.visibilityState === 'hidden' || !ref.current?.isConnected) return
      const style = getComputedStyle(ref.current)
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0 || ref.current.getClientRects().length === 0 || !members.current) return
      const boundary = members.current.getBoundingClientRect()
      for (const element of members.current.querySelectorAll<HTMLElement>('[data-notification-member]')) {
        const box = element.getBoundingClientRect()
        const inset = Math.min(2, box.height / 3, box.width / 3)
        const points = [[box.left + inset, box.top + inset], [box.right - inset, box.top + inset], [box.left + inset, box.bottom - inset], [box.right - inset, box.bottom - inset], [box.left + box.width / 2, box.top + box.height / 2]]
        const unobscured = typeof document.elementFromPoint === 'function' && points.every(([x, y]) => { const hit = document.elementFromPoint(x, y); return hit && element.contains(hit) })
        if (box.height > 0 && box.width > 0 && box.top >= Math.max(0, boundary.top) && box.bottom <= Math.min(innerHeight, boundary.bottom) && box.left >= 0 && box.right <= innerWidth && unobscured) seen.current.add(element.dataset.notificationMember!)
      }
      if (!batch.member_refs.every(member => seen.current.has(member.event_id))) return
      reported.current = true; void state.reportVisible(batch.id)
    }
    frame = requestAnimationFrame(report); document.addEventListener('visibilitychange', report); members.current?.addEventListener('scroll', report); window.addEventListener('resize', report)
    const container = members.current
    return () => { cancelAnimationFrame(frame); document.removeEventListener('visibilitychange', report); container?.removeEventListener('scroll', report); window.removeEventListener('resize', report) }
  }, [batch.id, state.reportVisible])
  return <div ref={ref} className={`pn-toast pn-kind-${batch.kind}`} role="status" aria-label="新通知">
    <span className="pn-toast-icon"><KindIcon kind={batch.kind} /></span><div><strong>{batch.entries[0]?.actor_name || 'Agent'} · {kindNames[batch.kind]}{batch.entries.length > 1 ? ` · ${batch.entries.length}` : ''}</strong><div ref={members} className="pn-toast-members">{batch.entries.map(entry => <span key={entry.id} data-notification-member={entry.id}>{entry.title || kindNames[entry.kind]}</span>)}</div></div><button aria-label="查看新通知" onClick={onOpen}><ChevronRight size={18} /></button><button aria-label="关闭新通知" onClick={state.dismissToast}><X size={15} /></button>
  </div>
}

export function PersonalNotifications({ api, namespace, connected, agents, open, onClose, onToggle, onOpenResult, onOpenItem, onOpenAction }: {
  api: PersonalNotificationsApi; namespace: string; connected: boolean; agents: PersonalAgent[]; open: boolean
  onClose: () => void; onToggle: () => void; onOpenResult: (id: string) => void; onOpenItem: (id: string) => void; onOpenAction: (id: string) => void
}) {
  const state = usePersonalNotifications({ api, namespace, connected, open }), [settings, setSettings] = useState(false)
  const drawer = useRef<HTMLElement>(null), bell = useRef<HTMLButtonElement>(null)
  useEffect(() => { setSettings(false) }, [namespace])
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    drawer.current?.querySelector<HTMLElement>('button,input,select')?.focus()
    return () => { if (previous?.isConnected) previous.focus(); else bell.current?.focus() }
  }, [open])
  const navigate = (entry: PersonalNotificationEntry) => {
    if (entry.availability === 'source_unavailable' || !entry.jump) return
    onClose()
    if (entry.jump.kind === 'result') onOpenResult(entry.jump.id)
    else if (entry.jump.kind === 'item') onOpenItem(entry.jump.id)
    else onOpenAction(entry.jump.id)
  }
  const unread = state.context?.unread_count || 0, quiet = state.context?.quiet
  const pending = state.pending || state.identityError
  return <div className="pn-root">
    <button ref={bell} type="button" className={`pn-bell${open ? ' selected' : ''}`} aria-label={unread ? `通知，${unread} 条未读` : '通知'} aria-expanded={open} aria-controls="personal-notifications" onClick={onToggle}>
      <Bell size={17} />{unread > 0 && <span className="pn-badge">{unread > 99 ? '99+' : unread}</span>}{quiet?.quiet && <span className="pn-quiet-dot" aria-label="勿扰中" />}
    </button>
    {state.toast && <ActiveToast key={state.toast.batch.id} batch={state.toast.batch} state={state} onOpen={() => { state.dismissToast(); if (!open) onToggle() }} />}
    {open && <><button className="pn-scrim" tabIndex={-1} aria-label="收起通知" onClick={onClose} /><section id="personal-notifications" ref={drawer} className="pn-drawer" role="dialog" aria-modal="true" aria-label="通知中心" onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); onClose() }
      if (event.key === 'Tab') { const controls = [...(drawer.current?.querySelectorAll<HTMLElement>('button,input,select,a[href]') || [])].filter(control => !(control as HTMLButtonElement).disabled && control.tabIndex >= 0); if (controls.length && (event.shiftKey ? document.activeElement === controls[0] : document.activeElement === controls.at(-1))) { event.preventDefault(); (event.shiftKey ? controls.at(-1) : controls[0])?.focus() } }
    }}>
      <header className="pn-heading"><div><span className="pn-heading-icon"><Bell size={20} /></span><h2>{settings ? '通知设置' : '通知'}</h2>{!settings && unread > 0 && <span className="pn-count">{unread}</span>}</div><div><button aria-label={settings ? '返回通知列表' : '通知设置'} aria-pressed={settings} disabled={!state.context} onClick={() => setSettings(value => !value)}>{settings ? <Inbox size={17} /> : <Settings2 size={17} />}</button><button aria-label="刷新通知" disabled={!connected || state.loading} onClick={state.refresh}><RefreshCw size={16} className={state.loading ? 'pn-spin' : ''} /></button><button aria-label="关闭通知中心" onClick={onClose}><X size={19} /></button></div></header>
      {!connected && <div className="pn-error" role="status">本地服务已断开</div>}
      {pending && <div className="pn-recovery" role="status"><div><strong>原通知请求待核对</strong>{state.pending && <code>{state.pending.request_id}</code>}{state.identityError && <span>{state.identityError}</span>}</div><button disabled={!state.pending || !connected || state.posting} onClick={() => void state.reconcile()}>核对原请求</button></div>}
      {state.error && <div className="pn-error" role="alert">{state.error}</div>}
      {state.context?.worker.last_error_code && <div className="pn-hold" role="status"><CircleAlert size={16} />通知扫描待恢复</div>}
      {quiet?.hold_reason && <div className="pn-hold" role="status"><CircleAlert size={16} />{quiet.hold_reason === 'clock_rollback' ? '时钟已回退，主动提醒暂停' : '时区数据已更新，请重新保存设置'}</div>}
      {!quiet?.hold_reason && quiet?.quiet && <div className="pn-quiet-state"><Moon size={15} /><span>勿扰中</span>{quiet.next_release && <time dateTime={quiet.next_release}>{when(quiet.next_release)} 结束</time>}</div>}
      {settings && state.context ? <NotificationSettings key={`${namespace}:settings`} preferences={state.context.preferences} actors={state.context.actors.length ? state.context.actors : [{ id: 'main', name: '主 Agent' }, ...agents.map(agent => ({ id: agent.id, name: agent.name }))]} locked={state.locked} onSave={state.savePreferences} /> : <>
        <div className="pn-toolbar"><div role="tablist" aria-label="通知筛选">{(['unread', 'attention', 'all'] as const).map(filter => <button role="tab" key={filter} aria-selected={filter === state.filter} onClick={() => state.changeFilter(filter)}>{({ unread: '未读', attention: '需处理', all: '全部' })[filter]}{filter === 'attention' && Boolean(state.context?.attention_count) && <b>{state.context?.attention_count}</b>}</button>)}</div><select aria-label="通知 Agent" value={state.actorId} onChange={event => state.changeActor(event.target.value)}><option value="">全部 Agent</option>{(state.context?.actors || []).map(actor => <option key={actor.id} value={actor.id}>{actor.name}</option>)}</select></div>
        {!state.context?.configured && connected && !state.loading && <div className="pn-configure"><BellOff size={21} /><strong>尚未开启通知</strong><button className="pn-primary" disabled={!state.context} onClick={() => setSettings(true)}>设置通知</button></div>}
        {state.context?.configured && !state.context.preferences.preferences.enabled && <div className="pn-paused"><BellOff size={14} />主动提醒已关闭</div>}
        <div className="pn-list" aria-label="通知列表" aria-busy={state.loading}>
          {state.loading && !state.page && <div className="pn-empty" role="status"><Loader2 size={23} className="pn-spin" /><span>正在读取通知</span></div>}
          {state.page?.entries.map(entry => <article className={`pn-entry pn-kind-${entry.kind}${entry.unread ? ' unread' : ''}${entry.availability === 'source_unavailable' ? ' unavailable' : ''}`} key={entry.id}>
            <span className="pn-entry-icon"><KindIcon kind={entry.kind} /></span><div className="pn-entry-body"><div className="pn-entry-meta"><span>{entry.actor_name || (entry.availability === 'source_unavailable' ? '来源不可用' : 'Agent')}</span><time dateTime={entry.occurred_at}>{when(entry.occurred_at)}</time></div><h3>{entry.title || (entry.availability === 'source_unavailable' ? '原来源已不可用' : kindNames[entry.kind])}</h3><div className="pn-entry-foot"><span>{entry.availability === 'resolved' ? '已处理' : kindNames[entry.kind]}</span><div>{entry.jump && entry.availability !== 'source_unavailable' && <button aria-label={`打开原${entry.jump.kind === 'calendar_action' ? '动作' : entry.jump.kind === 'result' ? '结果' : '事项'}：${entry.title || kindNames[entry.kind]}`} onClick={() => navigate(entry)}>查看<ArrowUpRight size={13} /></button>}{entry.unread ? <button disabled={state.locked} aria-label={`标记通知已读：${entry.title || '来源不可用'}`} onClick={() => void state.read([entry])}><Check size={13} />已读</button> : <CheckCheck size={14} aria-label="通知已读" />}</div></div></div>{entry.unread && <span className="pn-unread-dot" aria-label="未读通知" />}
          </article>)}
          {!state.loading && state.page && !state.page.entries.length && state.context?.configured && <div className="pn-empty"><Inbox size={32} /><strong>{state.filter === 'unread' ? '没有未读通知' : state.filter === 'attention' ? '没有待处理通知' : '暂无通知'}</strong></div>}
        </div>
        {(state.page?.partial || state.context?.partial) && <div className="pn-partial" role="status">部分来源待核对<button disabled={state.loading || !connected} onClick={state.refresh}>刷新</button></div>}
        {state.page?.next_after && <button className="pn-more" disabled={state.loading} onClick={() => void state.more()}>加载更多</button>}
        <footer className="pn-list-footer"><span>{state.page?.entries.length || 0} 条</span><button disabled={state.locked || !state.page?.entries.some(entry => entry.unread)} onClick={() => void state.read(state.page?.entries || [])}><CheckCheck size={14} />{(state.page?.entries.filter(entry => entry.unread).length || 0) > 100 ? '前 100 条通知已读' : '本页通知已读'}</button></footer>
      </>}
    </section></>}
  </div>
}
