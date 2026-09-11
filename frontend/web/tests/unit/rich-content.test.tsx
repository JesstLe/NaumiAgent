import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MessageContent, contentUrl } from '../../../web2/src/rich/MessageContent'

describe('rich message content', () => {
  it('uses spacious display fractions for same-line double dollars while preserving inline math and code', () => {
    const { container } = render(<MessageContent content={'行内 $\\frac{1}{2}$。\n\n$$\\sum_{i=1}^{n}i=\\frac{n(n+1)}{2}$$\n\n$$\n\\frac{a}{b}\n$$\n\n`$$literal$$`'} />)
    expect(container.querySelectorAll('.katex-display')).toHaveLength(2)
    expect(container.querySelectorAll('math[display="block"]')).toHaveLength(2)
    expect(container.querySelector('p > .katex:not(.katex-display)')).toBeTruthy()
    expect(screen.getByText('$$literal$$')).toBeInTheDocument()
  })
  it('renders Markdown, GFM, math and highlighted code', () => {
    const { container } = render(<MessageContent content={'## 结果\n\n**有效**\n\n| 产品 | 数量 |\n| --- | --- |\n| A | 3 |\n\n- [x] 已完成\n\n$$E=mc^2$$\n\n```python\nprint(42)\n```'} />)
    expect(screen.getByRole('heading', { name: '结果' })).toBeInTheDocument()
    expect(screen.getByRole('table')).toHaveTextContent('A')
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(container.querySelector('.katex')).toBeTruthy()
    expect(container.querySelector('.hljs-number')).toHaveTextContent('42')
    expect(screen.getByRole('button', { name: '下载代码' })).toBeInTheDocument()
  })
  it('does not render raw HTML or unsafe links', () => {
    const { container } = render(<MessageContent content={'<script>alert(1)</script>\n\n[危险](javascript:alert(1))\n\n![失败](file:///C:/private.png)'} />)
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(contentUrl('//evil.test')).toBe('')
    expect(contentUrl('https://user:secret@example.com/image')).toBe('')
  })
  it('handles empty, streaming code and invalid math without crashing', () => {
    const { rerender } = render(<MessageContent content="" />)
    rerender(<MessageContent content={'```python\nprint('} />)
    expect(screen.getByText('print(')).toBeInTheDocument()
    rerender(<MessageContent content={'$$\\notacommand{$$'} />)
    expect(screen.getByText(/\\notacommand/)).toBeInTheDocument()
  })
  it('opens and closes an image and gives a recoverable load error', () => {
    render(<MessageContent content={'![分析结果](https://example.com/chart.png)'} />)
    fireEvent.click(screen.getByRole('button', { name: '放大图片：分析结果' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭图片' }))
    fireEvent.error(screen.getByRole('img'))
    expect(screen.getByRole('status')).toHaveTextContent('图片无法加载')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(screen.getByRole('img')).toBeInTheDocument()
  })
})
