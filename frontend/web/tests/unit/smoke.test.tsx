import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import App from '@/App'

vi.mock('@/hooks/useWorkbenchConnection', () => ({
  useWorkbenchConnection: () => ({
    client: null,
    status: { isConnected: true, daemon: null, error: null },
    currentSessionId: 'test-session',
    snapshot: null,
    isReady: true,
    error: null,
    connect: vi.fn().mockResolvedValue(undefined),
    selectSession: vi.fn().mockResolvedValue(undefined),
    bootstrap: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn(),
  }),
}))

describe('App shell', () => {
  it('renders the chat page as the default view', () => {
    render(<App />)
    expect(screen.getByPlaceholderText('输入问题或指令...')).toBeInTheDocument()
  })
})
