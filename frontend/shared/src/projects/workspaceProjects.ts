import type { PlatformAdapter } from '../platform/PlatformAdapter'

export type WorkspaceProjectLocation = 'local' | 'remote'

export interface WorkspaceProject {
  id: string
  name: string
  path: string
  location: WorkspaceProjectLocation
  createdAt: string
  lastOpenedAt: string
}

const STORAGE_KEY = 'workspace-projects-v1'

function trimWorkspacePath(path: string): string {
  const value = path.trim()
  if (/^\/+$/u.test(value)) return '/'
  if (/^[A-Za-z]:[\\/]*$/.test(value))
    return `${value.slice(0, 2)}${String.fromCharCode(92)}`
  return value.replace(/[\\/]+$/, '')
}

export function normalizeWorkspacePath(path: string): string {
  const trimmed = trimWorkspacePath(path)
  return /^[A-Za-z]:/.test(trimmed) || trimmed.startsWith('\\\\')
    ? trimmed.toLowerCase()
    : trimmed
}

function projectId(location: WorkspaceProjectLocation, path: string) {
  return `${location}:${normalizeWorkspacePath(path)}`
}

function validProject(value: unknown): value is WorkspaceProject {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<WorkspaceProject>
  return (
    typeof item.id === 'string' &&
    typeof item.name === 'string' &&
    !!item.name.trim() &&
    typeof item.path === 'string' &&
    !!item.path.trim() &&
    (item.location === 'local' || item.location === 'remote') &&
    typeof item.createdAt === 'string' &&
    typeof item.lastOpenedAt === 'string'
  )
}

export async function loadWorkspaceProjects(
  platform: PlatformAdapter,
): Promise<WorkspaceProject[]> {
  const stored = await platform.getSetting(STORAGE_KEY)
  if (!stored) return []
  try {
    const parsed = JSON.parse(stored)
    if (!Array.isArray(parsed)) return []
    const unique = new Map<string, WorkspaceProject>()
    for (const item of parsed) {
      if (!validProject(item)) continue
      const id = projectId(item.location, item.path)
      const normalized = { ...item, id, name: item.name.trim(), path: item.path.trim() }
      const previous = unique.get(id)
      if (!previous || normalized.lastOpenedAt > previous.lastOpenedAt)
        unique.set(id, normalized)
    }
    return [...unique.values()].sort((left, right) =>
      right.lastOpenedAt.localeCompare(left.lastOpenedAt),
    )
  } catch {
    return []
  }
}

export async function saveWorkspaceProject(
  platform: PlatformAdapter,
  input: {
    name: string
    path: string
    location?: WorkspaceProjectLocation
    openedAt?: string
  },
): Promise<WorkspaceProject[]> {
  const name = input.name.trim()
  const path = trimWorkspacePath(input.path)
  if (!name) throw new Error('请输入项目名称')
  if (!path) throw new Error('请选择项目文件夹')
  const location = input.location ?? 'local'
  const openedAt = input.openedAt ?? new Date().toISOString()
  const id = projectId(location, path)
  const existing = await loadWorkspaceProjects(platform)
  const previous = existing.find((project) => project.id === id)
  const project: WorkspaceProject = {
    id,
    name,
    path,
    location,
    createdAt: previous?.createdAt ?? openedAt,
    lastOpenedAt: openedAt,
  }
  const next = [project, ...existing.filter((item) => item.id !== id)]
  await platform.setSetting(STORAGE_KEY, JSON.stringify(next))
  return next
}

export function workspaceProjectMatches(
  project: WorkspaceProject,
  path: string | undefined,
) {
  return !!path && normalizeWorkspacePath(project.path) === normalizeWorkspacePath(path)
}
