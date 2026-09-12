import { useState } from 'react'
import { CheckCircle2, ChevronDown, Circle, CircleDashed, RefreshCw, Plus } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import type { Todo } from '@naumi/shared/api/WorkbenchRuntimeClient'

const statusNames: Record<Todo['status'], string> = { pending: '待处理', in_progress: '进行中', blocked: '受阻', completed: '已完成' }
export function TodoPanel() {
  const w = useWorkspace()
  const [subject, setSubject] = useState('')
  const [dependencies, setDependencies] = useState('')
  const [showCompleted, setShowCompleted] = useState(false)
  const completed = w.todos.filter(todo => todo.status === 'completed')
  const open = w.todos.filter(todo => todo.status !== 'completed')
  const done = completed.length
  const locked = (w.busy && w.runningSessionId === w.sessionId) || w.uploading || w.tasksMutating || !w.daemon
  const rows = (todos: Todo[]) => <>{todos.map(todo => <div className={`w2-todo ${todo.status}`} key={todo.id}>
    {todo.status === 'completed' ? <CheckCircle2 size={16} /> : todo.status === 'in_progress' ? <CircleDashed size={16} className="w2-spin" /> : <Circle size={16} />}
    <div><span><small>#{todo.id} </small>{todo.subject}</span>{todo.active_form && todo.status !== 'completed' && <small>{todo.active_form}</small>}
      {!!todo.blocked_by.length && <small>依赖 {todo.blocked_by.map(id => `#${id}`).join('、')}</small>}
    </div>
    <select aria-label={`待办状态 ${todo.subject}`} value={todo.status} disabled={locked || todo.status === 'completed'} onChange={event => void w.updateTodo(todo.id, event.target.value as Todo['status'])}>
      {Object.entries(statusNames).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
    </select>
  </div>)}</>
  return <div className="w2-todos">
    <div className="w2-section-heading"><span>待办 <small>{open.length ? `${open.length} 项未完成` : '当前无待办'}</small></span><button aria-label="刷新待办" disabled={w.tasksLoading || w.tasksMutating} onClick={() => void w.refreshTasks()}><RefreshCw size={15} /></button></div>
    {!!open.length && <progress aria-label="待办完成进度" max={w.todos.length || 1} value={done} />}
    {w.taskError && <p role="alert" className="w2-error">{w.taskError}</p>}
    {!w.todos.length && <p className="w2-muted">{w.tasksLoading ? '正在读取待办…' : '暂无待办'}</p>}
    {!open.length && !!completed.length && <div className="w2-todo-complete" role="status"><CheckCircle2 size={17} /><div><strong>本轮待办已全部完成</strong><small>{completed.length} 项已从活跃列表收起</small></div></div>}
    {rows(open)}
    {!!completed.length && <>
      <button className="w2-completed-toggle" type="button" aria-expanded={showCompleted} aria-controls="w2-completed-todos" onClick={() => setShowCompleted(value => !value)}>
        <ChevronDown size={14} />已完成 {completed.length} 项
      </button>
      {showCompleted && <div id="w2-completed-todos" className="w2-completed-list">{rows(completed)}</div>}
    </>}
    <form className="w2-todo-form" onSubmit={async event => { event.preventDefault(); if (await w.addTodo(subject, dependencies.split(/[,，\s]+/).filter(Boolean))) { setSubject(''); setDependencies('') } }}>
      <label>新增待办<input aria-label="新增待办" required maxLength={500} value={subject} onChange={event => setSubject(event.target.value)} placeholder="下一步要做什么？" /></label>
      <label>依赖任务<input aria-label="依赖任务" value={dependencies} onChange={event => setDependencies(event.target.value)} placeholder="任务编号，逗号分隔（可选）" /></label>
      <button className="w2-primary" disabled={locked || !subject.trim()}><Plus size={14} />{w.tasksMutating ? '保存中…' : '添加待办'}</button>
    </form>
  </div>
}
