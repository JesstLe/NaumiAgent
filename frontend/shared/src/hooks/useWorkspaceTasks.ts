import { useCallback, useEffect, useRef, useState } from 'react'
import type { WorkbenchRuntimeClient, Todo } from '../api/WorkbenchRuntimeClient'

export function useWorkspaceTasks(api: WorkbenchRuntimeClient, sessionId: string | null, connected: boolean, ensureSession: () => Promise<string>) {
  const [todos, setTodos] = useState<Todo[]>([])
  const [taskError, setTaskError] = useState('')
  const [tasksLoading, setTasksLoading] = useState(false)
  const [tasksMutating, setTasksMutating] = useState(false)
  const pending = useRef(false)
  const revision = useRef(0)
  const currentSession = useRef(sessionId)
  currentSession.current = sessionId
  const refreshTasks = useCallback(async () => {
    if (!sessionId || !connected) return
    const version = ++revision.current
    setTasksLoading(true)
    try {
      const response = await api.todos(sessionId)
      if (revision.current === version) { setTodos(response.todos ?? []); setTaskError('') }
    } catch (error) {
      if (revision.current === version) setTaskError(error instanceof Error ? error.message : '待办加载失败')
    } finally { if (revision.current === version) setTasksLoading(false) }
  }, [api, sessionId, connected])
  useEffect(() => {
    setTodos([]); setTaskError(''); setTasksLoading(false)
    void refreshTasks()
    const timer = setInterval(() => { if (!document.hidden && !pending.current) void refreshTasks() }, 10000)
    return () => { revision.current++; clearInterval(timer) }
  }, [refreshTasks])
  const mutate = async (operation: (id: string) => Promise<{ todos: Todo[] }>) => {
    if (pending.current) return false
    pending.current = true; setTasksMutating(true); setTaskError('')
    let id: string | null = sessionId
    try {
      id = await ensureSession()
      const response = await operation(id)
      if (currentSession.current === id) { revision.current++; setTasksLoading(false); setTodos(response.todos) }
      return true
    } catch (error) {
      if (currentSession.current === id) setTaskError(error instanceof Error ? error.message : '待办未保存，请重试')
      return false
    } finally { pending.current = false; setTasksMutating(false) }
  }
  return { todos, taskError, tasksLoading, tasksMutating, refreshTasks,
    addTodo: (subject: string, blocked_by: string[] = []) => mutate(id => api.createTodo(id, { subject, blocked_by })),
    updateTodo: (taskId: string, status: Todo['status']) => mutate(id => api.updateTodo(id, taskId, status)),
  }
}
