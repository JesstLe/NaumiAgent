import { create } from 'zustand'

interface ConnectionState {
  isConnected: boolean
  isLoading: boolean
  error: string | null
  setConnected: (connected: boolean) => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
}

export const useConnectionStore = create<ConnectionState>((set) => ({
  isConnected: false,
  isLoading: false,
  error: null,
  setConnected: (connected) => set({ isConnected: connected }),
  setLoading: (loading) => set({ isLoading: loading }),
  setError: (error) => set({ error }),
}))
