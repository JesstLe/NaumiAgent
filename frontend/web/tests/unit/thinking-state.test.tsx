import { render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { ThinkingState } from '../../../web2/src/beautiful/ThinkingState'

const workspace = {
  daemon: { workspace_root: 'E:/Workspace/NaumiAgent' },
  busy: true,
  liveEvents: [
    { id: '1', type: 'turn_start', turn: 1, sequence: 1, data: {} },
    { id: '2', type: 'context_compacted', turn: 1, sequence: 2, data: { activity_summary: '已压缩上下文：41 → 4 条消息' } },
    { id: '3', type: 'phase_summary', turn: 1, sequence: 3, data: { activity_summary: '本阶段已完成：已要求继续调用写入工具。' } },
    { id: '4', type: 'turn_start', turn: 2, sequence: 4, data: {} },
    { id: '5', type: 'phase_summary', turn: 2, sequence: 5, data: { activity_summary: '本阶段已完成：已自动压缩上下文并重新执行。' } },
    { id: '6', type: 'thinking_start', turn: 3, sequence: 6, data: {} },
  ],
  sessionId: 'session',
}

vi.mock('@naumi/shared/hooks/WorkspaceProvider', () => ({
  useWorkspace: () => workspace,
}))

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  })
})

describe('Web2 execution trace', () => {
  it('keeps recovery notes and current reasoning inside one running phase', () => {
    const { container } = render(<ThinkingState live objective="创建高级页面" />)

    expect(screen.getAllByRole('button', { name: '正在推理' })).toHaveLength(1)
    expect(container.querySelectorAll('.bui-stage')).toHaveLength(1)
    expect(container.textContent?.match(/本次任务：/g)).toHaveLength(1)
    expect(container).toHaveTextContent('已压缩上下文：41 → 4 条消息')
    expect(container).not.toHaveTextContent('执行进度已更新 · 已完成')
  })

  it('reserves task completion wording for a terminal completed run', () => {
    workspace.busy = false
    workspace.liveEvents = []
    render(<ThinkingState run={{
      id: 'run',
      status: 'completed',
      started_at: '2026-09-11T10:00:00Z',
      completed_at: '2026-09-11T10:00:02Z',
      steps: [
        { sequence: 1, stage: 'request', status: 'completed', summary: '检查页面', detail: '' },
        { sequence: 2, stage: 'analysis', status: 'completed', summary: '第 1 轮分析', detail: '' },
      ],
    }} />)

    expect(screen.getByRole('button', { name: '任务已完成 · 用时 2 秒' })).toBeInTheDocument()
  })
})
