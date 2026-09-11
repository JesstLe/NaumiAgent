import { useState } from 'react'
import { Target, RefreshCw } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import type { WorkspaceGoal } from '@naumi/shared/api/WorkbenchRuntimeClient'

const names: Record<WorkspaceGoal['status'], string> = { active: '进行中', paused: '已暂停', blocked: '受阻', completed: '已完成', cancelled: '已取消' }
export function GoalPanel() {
  const w = useWorkspace()
  const [objective, setObjective] = useState('')
  const [change, setChange] = useState<{ goal: WorkspaceGoal; status: WorkspaceGoal['status'] } | null>(null)
  const [note, setNote] = useState('')
  const [history, setHistory] = useState(false)
  const current = w.goalSnapshot?.goals?.find(goal => goal.goal_id === w.goalSnapshot?.current_goal_id)
  const locked = (w.busy && w.runningSessionId === w.sessionId) || w.uploading || w.goalsMutating || !w.daemon
  const goals = (w.goalSnapshot?.goals ?? []).filter(goal => history || goal.goal_id === current?.goal_id)
  return <div className="w2-goals">
    <div className="w2-section-heading"><span>持久目标</span><button aria-label="刷新目标" disabled={w.goalsMutating} onClick={() => void w.refreshGoals()}><RefreshCw size={15} /></button></div>
    <div className="w2-goal-tabs"><button aria-pressed={!history} onClick={() => setHistory(false)}>当前</button><button aria-pressed={history} onClick={() => setHistory(true)}>历史</button><small>工作区共享</small></div>
    {w.goalError && <p role="alert" className="w2-error">{w.goalError}</p>}
    {w.goalSnapshot?.warnings?.map(message => <p role="alert" className="w2-error" key={message}>{message}</p>)}
    {goals.map(goal => <article className="w2-goal-card" key={goal.goal_id}>
      <header><Target size={16} /><span>{names[goal.status]}</span><time>{new Date(goal.updated_at).toLocaleString('zh-CN')}</time></header>
      <h3>{goal.objective}</h3>{goal.note && <p>{goal.note}</p>}
      {goal.session_id && goal.session_id !== w.sessionId && <button className="w2-goal-link" onClick={() => void w.select(goal.session_id)}>打开关联会话</button>}
      {goal.pursuit && <div className="w2-pursuit">
        <div>已验证标准 {goal.pursuit.criteria_verified} / {goal.pursuit.criteria_total} · 第 {goal.pursuit.iteration} 轮</div>
        <progress max={goal.pursuit.criteria_total || 1} value={goal.pursuit.criteria_verified} />
        {goal.pursuit.blocked_reason && <p>{goal.pursuit.blocked_reason}</p>}
        {goal.pursuit.next_action && <p>下一步：{goal.pursuit.next_action}</p>}
        {goal.pursuit.evidence.map((item, index) => <details key={index}><summary>{item.summary}</summary><p>{item.source}</p></details>)}
      </div>}
      {goal.pursuit_run_id && <p className="w2-muted">已关联追踪运行；生命周期由追踪控制流程管理。</p>}
      {!['completed', 'cancelled'].includes(goal.status) && !goal.pursuit_run_id && <div className="w2-goal-actions">
        {(['active', 'paused', 'blocked', 'completed', 'cancelled'] as const).filter(status => status !== goal.status).map(status => <button key={status} disabled={locked} onClick={() => { setChange({ goal, status }); setNote('') }}>
          {{ active: '恢复', paused: '暂停', blocked: '标记受阻', completed: '完成', cancelled: '取消目标' }[status]}
        </button>)}
      </div>}
      {change?.goal.goal_id === goal.goal_id && <form className="w2-goal-confirm" onSubmit={async event => {
        event.preventDefault(); if (await w.updateGoal(goal.goal_id, change.status, note)) setChange(null)
      }}><strong>将目标设为“{names[change.status]}”？</strong>
        <label>变更说明<textarea required={change.status === 'blocked'} maxLength={4000} value={note} onChange={event => setNote(event.target.value)} /></label>
        <div><button type="button" onClick={() => setChange(null)}>返回</button><button disabled={locked}>确认变更</button></div>
      </form>}
    </article>)}
    {!current && !history && <form className="w2-todo-form w2-goal-form" onSubmit={async event => {
      event.preventDefault(); if (await w.addGoal(objective)) setObjective('')
    }}><p className="w2-muted">暂无进行中的目标</p><label>目标内容<textarea aria-label="目标内容" required maxLength={8000} rows={4} value={objective} onChange={event => setObjective(event.target.value)} placeholder="希望持续推进的目标" /></label>
      <button className="w2-primary" disabled={locked || !objective.trim()}>创建目标</button>
    </form>}
    {history && !goals.length && <p className="w2-muted">暂无历史目标</p>}
    {w.goalSnapshot?.truncated && <p className="w2-muted">显示最近 20 个目标</p>}
  </div>
}
