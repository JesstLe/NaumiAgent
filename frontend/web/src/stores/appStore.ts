import { create } from 'zustand'

export type AppRoute =
  | 'dashboard'
  | 'chat'
  | 'taskMarket'
  | 'worktrees'
  | 'reviews'
  | 'timeline'
  | 'settings'

interface AppState {
  currentRoute: AppRoute
  connectionStatus: 'online' | 'offline' | 'starting' | 'unknown'
  openIssues: number
  activeAgents: number
  setCurrentRoute: (route: AppRoute) => void
  setConnectionStatus: (status: AppState['connectionStatus']) => void
}

export const useAppStore = create<AppState>((set) => ({
  currentRoute: 'chat',
  connectionStatus: 'unknown',
  openIssues: 0,
  activeAgents: 0,
  setCurrentRoute: (route) => set({ currentRoute: route }),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}))
