import { create } from 'zustand'
import type { Session, MessageResponse, Issue, Mission, Worktree, AgentProfileEnriched, WorkbenchSnapshot, Event } from '@/api/types'

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
  setLoading: (isLoading) => set({ isLoading }),
  setError: (error) => set({ error }),
}))
