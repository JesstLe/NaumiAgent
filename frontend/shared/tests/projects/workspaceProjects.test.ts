import { describe, expect, it } from 'vitest'
import type { PlatformAdapter } from '../../src/platform/PlatformAdapter'
import {
  loadWorkspaceProjects,
  saveWorkspaceProject,
  workspaceProjectMatches,
} from '../../src/projects/workspaceProjects'

function memoryPlatform(): PlatformAdapter {
  const settings = new Map<string, string>()
  return {
    supportsDaemon: false,
    supportsShell: false,
    getSetting: async (key) => settings.get(key) ?? null,
    setSetting: async (key, value) => { settings.set(key, value) },
    getToken: async () => null,
    setToken: async () => {},
    removeToken: async () => {},
    log: async () => {},
  }
}

describe('workspace project registry', () => {
  it('persists aliases, deduplicates Windows paths and orders recent projects first', async () => {
    const platform = memoryPlatform()
    await saveWorkspaceProject(platform, {
      name: 'NaumiAgent',
      path: 'E:\\Workspace\\NaumiAgent\\',
      openedAt: '2026-09-11T10:00:00Z',
    })
    await saveWorkspaceProject(platform, {
      name: 'NaumiAgent 新名称',
      path: 'e:\\workspace\\naumiagent',
      openedAt: '2026-09-11T11:00:00Z',
    })
    await saveWorkspaceProject(platform, {
      name: '另一个项目',
      path: 'E:\\Workspace\\Other',
      openedAt: '2026-09-11T12:00:00Z',
    })

    const projects = await loadWorkspaceProjects(platform)
    expect(projects).toHaveLength(2)
    expect(projects.map((project) => project.name)).toEqual(['另一个项目', 'NaumiAgent 新名称'])
    expect(workspaceProjectMatches(projects[1], 'E:\\WORKSPACE\\NAUMIAGENT\\')).toBe(true)
  })

  it('ignores corrupt persisted data', async () => {
    const platform = memoryPlatform()
    await platform.setSetting('workspace-projects-v1', '{broken')
    await expect(loadWorkspaceProjects(platform)).resolves.toEqual([])
  })

  it('preserves a Windows drive root as an absolute project path', async () => {
    const platform = memoryPlatform()
    const projects = await saveWorkspaceProject(platform, {
      name: 'E 盘',
      path: 'E:\\',
    })
    expect(projects[0].path).toBe('E:\\')
    expect(workspaceProjectMatches(projects[0], 'e:/')).toBe(true)
  })
})
