import { create } from 'zustand'

export type AppRoute =
  | 'dashboard'
  | 'chat'
  | 'taskMarket'
  | 'worktrees'
  | 'reviews'
  | 'timeline'
  | 'settings'

export type SideTool = 'projects' | 'groups' | 'skills' | 'search'

interface AppState {
  currentRoute: AppRoute
  connectionStatus: 'online' | 'offline' | 'starting' | 'unknown'
  openIssues: number
  activeAgents: number
  activeSideTool: SideTool
  setCurrentRoute: (route: AppRoute) => void
  setConnectionStatus: (status: AppState['connectionStatus']) => void
  setActiveSideTool: (tool: SideTool) => void
}

export const useAppStore = create<AppState>((set) => ({
  currentRoute: 'chat',
  connectionStatus: 'unknown',
  openIssues: 0,
  activeAgents: 0,
  activeSideTool: 'projects',
  setCurrentRoute: (route) => set({ currentRoute: route }),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
  setActiveSideTool: (tool) => set({ activeSideTool: tool }),
}))
