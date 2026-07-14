import { Routes, Route, Navigate } from 'react-router-dom'
import { MainLayout } from '@/components/Shell/MainLayout'
import { ChatPage } from '@/components/Chat/ChatPage'
import { DashboardPage } from '@/components/Dashboard/DashboardPage'
import { TaskMarketPage } from '@/components/TaskMarket/TaskMarketPage'
import { WorktreesPage } from '@/components/Worktrees/WorktreesPage'
import { ReviewsPage } from '@/components/Reviews/ReviewsPage'
import { TimelinePage } from '@/components/Timeline/TimelinePage'
import { SettingsPage } from '@/components/Settings/SettingsPage'

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<MainLayout />}>
        <Route index element={<Navigate to="/chat" replace />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="task-market" element={<TaskMarketPage />} />
        <Route path="worktrees" element={<WorktreesPage />} />
        <Route path="reviews" element={<ReviewsPage />} />
        <Route path="timeline" element={<TimelinePage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  )
}
