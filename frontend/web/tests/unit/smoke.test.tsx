import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import App from '@/App'

describe('App shell', () => {
  it('renders the chat page as the default view', () => {
    render(<App />)
    expect(screen.getByPlaceholderText('输入问题或指令...')).toBeInTheDocument()
  })
})
