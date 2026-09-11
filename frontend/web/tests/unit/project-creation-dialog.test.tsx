import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { ProjectCreationDialog } from '../../../web2/src/ProjectCreationDialog'

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute('open')
  }
})

describe('ProjectCreationDialog', () => {
  it('selects a real local folder, infers its name and creates the project', async () => {
    const select = vi.fn().mockResolvedValue('E:\\Workspace\\DemoProject')
    const create = vi.fn().mockResolvedValue(undefined)
    render(
      <ProjectCreationDialog
        open
        canSelectLocal
        busy={false}
        initialPath={'E:\\Workspace'}
        onClose={vi.fn()}
        onSelectDirectory={select}
        onCreate={create}
      />,
    )

    expect(screen.getByRole('radio', { name: /本地/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: /远程/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '下一步' }))
    fireEvent.click(screen.getByRole('button', { name: '添加 NaumiAgent 可读取和编辑的文件夹' }))
    await waitFor(() => expect(select).toHaveBeenCalledWith('E:\\Workspace'))
    expect(await screen.findByDisplayValue('DemoProject')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '创建项目' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith({
      name: 'DemoProject',
      path: 'E:\\Workspace\\DemoProject',
      location: 'local',
    }))
  })

  it('explains why local project creation is unavailable in browser mode', () => {
    render(
      <ProjectCreationDialog
        open
        canSelectLocal={false}
        busy={false}
        onClose={vi.fn()}
        onSelectDirectory={vi.fn().mockResolvedValue(null)}
        onCreate={vi.fn().mockResolvedValue(undefined)}
      />,
    )
    expect(screen.getByText('本地项目创建需要 NaumiAgent 桌面版。')).toBeVisible()
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled()
  })
})
