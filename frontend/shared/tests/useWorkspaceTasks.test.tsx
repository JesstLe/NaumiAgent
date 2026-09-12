import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Todo, WorkbenchRuntimeClient } from '../src/api/WorkbenchRuntimeClient'
import { useWorkspaceTasks } from '../src/hooks/useWorkspaceTasks'

const createdTodo: Todo = {
  id: 'todo-created',
  subject: '新任务',
  description: '',
  status: 'pending',
  active_form: null,
  blocked_by: [],
  updated_at: '',
}

describe('useWorkspaceTasks', () => {
  it('keeps the mutation result when ensureSession creates the active session', async () => {
    let activeSessionId: string | null = null
    const api = {
      createTodo: vi.fn().mockResolvedValue({ todos: [createdTodo] }),
    } as unknown as WorkbenchRuntimeClient
    const { result } = renderHook(() => useWorkspaceTasks(
      api,
      null,
      true,
      async () => {
        activeSessionId = 'created'
        return 'created'
      },
      () => activeSessionId,
    ))

    await act(async () => {
      expect(await result.current.addTodo('新任务')).toBe(true)
    })

    expect(result.current.todos).toEqual([createdTodo])
  })

  it('does not apply a mutation response after the user switches sessions', async () => {
    let activeSessionId: string | null = 'one'
    let finish: ((value: { todos: Todo[] }) => void) | undefined
    const api = {
      createTodo: vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve })),
    } as unknown as WorkbenchRuntimeClient
    const { result } = renderHook(() => useWorkspaceTasks(
      api,
      'one',
      true,
      async () => 'one',
      () => activeSessionId,
    ))

    let mutation: Promise<boolean> | undefined
    await act(async () => {
      mutation = result.current.addTodo('新任务')
      await Promise.resolve()
    })
    expect(finish).toBeDefined()
    activeSessionId = 'two'
    await act(async () => {
      finish!({ todos: [createdTodo] })
      await mutation
    })

    expect(result.current.todos).toEqual([])
  })
})
