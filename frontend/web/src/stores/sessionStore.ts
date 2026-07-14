import { create } from 'zustand'
import type { Session, MessageResponse, Issue, Mission, Worktree, AgentProfileEnriched, WorkbenchSnapshot, Event } from '@/api/types'

export interface PendingPermission {
  call_id: string
  agent_name: string
  tool_name: string
  reason: string
  risk_level?: string
}

export interface SessionState {
  sessions: Session[]
  currentSessionId: string | null
  snapshot: WorkbenchSnapshot | null
  messages: MessageResponse[]
  missions: Mission[]
  issues: Issue[]
  worktrees: Worktree[]
  agents: AgentProfileEnriched[]
  events: Event[]
  pendingPermissions: PendingPermission[]
  isLoading: boolean
  error: string | null

  setSessions: (sessions: Session[]) => void
  setCurrentSessionId: (id: string | null) => void
  setSnapshot: (snapshot: WorkbenchSnapshot | null) => void
  setMessages: (messages: MessageResponse[]) => void
  appendMessage: (message: MessageResponse) => void
  setMissions: (missions: Mission[]) => void
  setIssues: (issues: Issue[]) => void
  setWorktrees: (worktrees: Worktree[]) => void
  setAgents: (agents: AgentProfileEnriched[]) => void
  setEvents: (events: Event[]) => void
  addPendingPermission: (permission: PendingPermission) => void
  removePendingPermission: (callId: string) => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
}

export const useSessionStore = create<SessionState>((set) => ({
  sessions: [],
  currentSessionId: null,
  snapshot: null,
  messages: [],
  missions: [],
  issues: [],
  worktrees: [],
  agents: [],
  events: [],
  pendingPermissions: [],
  isLoading: false,
  error: null,

  setSessions: (sessions) => set({ sessions }),
  setCurrentSessionId: (currentSessionId) => set({ currentSessionId }),
  setSnapshot: (snapshot) => set({ snapshot }),
  setMessages: (messages) => set({ messages }),
  appendMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  setMissions: (missions) => set({ missions }),
  setIssues: (issues) => set({ issues }),
  setWorktrees: (worktrees) => set({ worktrees }),
  setAgents: (agents) => set({ agents }),
  setEvents: (events) => set({ events }),
  addPendingPermission: (permission) =>
    set((state) => ({
      pendingPermissions: [
        ...state.pendingPermissions.filter((p) => p.call_id !== permission.call_id),
        permission,
      ],
    })),
  removePendingPermission: (callId) =>
    set((state) => ({
      pendingPermissions: state.pendingPermissions.filter((p) => p.call_id !== callId),
    })),
  setLoading: (isLoading) => set({ isLoading }),
  setError: (error) => set({ error }),
}))
