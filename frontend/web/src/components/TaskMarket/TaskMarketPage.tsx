import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2, Plus, Hand, Unlock } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import { formatDate } from '@/utils/formatDate'
import type { Issue, Lease, RiskLevel, ParallelMode } from '@/api/types'

const RISK_COLOR: Record<RiskLevel, string> = {
  low: 'bg-success/15 text-success',
  medium: 'bg-warning/15 text-warning',
  high: 'bg-danger/15 text-danger',
  critical: 'bg-danger/20 text-danger border border-danger/30',
}

export function TaskMarketPage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const setError = useSessionStore((state) => state.setError)
  const [issues, setIssues] = useState<Issue[]>([])
  const [leases, setLeases] = useState<Lease[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [actionTaskId, setActionTaskId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)

  // Seed from snapshot, then refresh from the API.
  useEffect(() => {
    setIssues(snapshot?.issues ?? [])
    setLeases(snapshot?.leases ?? [])
  }, [snapshot])

  useEffect(() => {
    if (!client || !currentSessionId) {
      setIssues([])
      setLeases([])
      return
    }
    let cancelled = false
    setIsLoading(true)
    const refresh = async () => {
      try {
        const [issueResp, leaseResp] = await Promise.all([
          client.fetchIssues(currentSessionId, { limit: 100 }),
          client.fetchLeases(currentSessionId, { state: 'active', limit: 100 }),
        ])
        if (!cancelled) {
          setIssues(issueResp.issues)
          setLeases(leaseResp.leases)
        }
      } catch (err) {
        if (!cancelled) setError(isApiException(err) ? err.message : String(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    void refresh()
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId, setError])

  // Map task_id -> active lease for quick lookup.
  const activeLeaseByTask = useMemo(() => {
    const map = new Map<string, Lease>()
    for (const lease of leases) {
      if (lease.state === 'active') map.set(lease.task_id, lease)
    }
    return map
  }, [leases])

  const handleClaim = async (issue: Issue) => {
    if (!client || !currentSessionId) return
    setActionTaskId(issue.task_id)
    setNotice(null)
    try {
      const lease = await client.claimIssue(currentSessionId, issue.task_id)
      setLeases((prev) => [...prev.filter((l) => l.task_id !== issue.task_id), lease])
      setNotice(t('taskMarket.claimSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionTaskId(null)
    }
  }

  const handleRelease = async (issue: Issue) => {
    if (!client || !currentSessionId) return
    const lease = activeLeaseByTask.get(issue.task_id)
    if (!lease) return
    setActionTaskId(issue.task_id)
    setNotice(null)
    try {
      const released = await client.releaseLease(currentSessionId, lease.id)
      setLeases((prev) => prev.map((l) => (l.id === released.id ? released : l)))
      setNotice(t('taskMarket.releaseSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionTaskId(null)
    }
  }

  const handleCreate = async (data: {
    missionId: string
    title: string
    description: string
    riskLevel: RiskLevel
    parallelMode: ParallelMode
  }) => {
    if (!client || !currentSessionId) return
    setNotice(null)
    try {
      const created = await client.createIssue(currentSessionId, data.missionId, {
        title: data.title,
        description: data.description || undefined,
        risk_level: data.riskLevel,
        parallel_mode: data.parallelMode,
      })
      setIssues((prev) => [...prev, created])
      setShowCreate(false)
      setNotice(t('taskMarket.createSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    }
  }

  if (!currentSessionId) {
    return (
      <div className="p-6 h-full overflow-y-auto">
        <h1 className="text-xl font-semibold text-text mb-4">{t('nav.taskMarket')}</h1>
        <p className="text-text-secondary">{t('taskMarket.empty')}</p>
      </div>
    )
  }

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold text-text">{t('nav.taskMarket')}</h1>
          {isLoading && <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-text-secondary">{issues.length} issues</span>
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover"
          >
            <Plus className="w-4 h-4" />
            {t('taskMarket.createIssue')}
          </button>
        </div>
      </div>

      {notice && (
        <div className="mb-4 rounded-md border border-success/30 bg-success/10 px-4 py-2 text-sm text-success">
          {notice}
        </div>
      )}

      {showCreate && (
        <CreateIssueForm
          missions={snapshot?.missions ?? []}
          onSubmit={handleCreate}
          onCancel={() => setShowCreate(false)}
        />
      )}

      {issues.length === 0 ? (
        <div className="text-text-secondary">{t('taskMarket.empty')}</div>
      ) : (
        <div className="space-y-3">
          {issues.map((issue) => {
            const lease = activeLeaseByTask.get(issue.task_id)
            const isClaimed = !!lease
            const isBusy = actionTaskId === issue.task_id
            return (
              <div key={issue.task_id} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <div className="font-medium text-text">{issue.task?.subject || issue.task_id}</div>
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${RISK_COLOR[issue.risk_level]}`}>
                        {issue.risk_level}
                      </span>
                      <span className="rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary capitalize">
                        {issue.parallel_mode}
                      </span>
                      {isClaimed && (
                        <span className="rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent">
                          {t('taskMarket.claimed')} · {lease?.agent_id}
                        </span>
                      )}
                    </div>
                    {issue.task?.description && (
                      <p className="mt-2 text-sm text-text-secondary line-clamp-2">{issue.task.description}</p>
                    )}
                    {isClaimed && lease && (
                      <p className="mt-1 text-xs text-text-secondary">
                        {formatDate(lease.created_at)} · {t('taskMarket.claimed')}
                      </p>
                    )}
                  </div>
                  <div className="flex-shrink-0">
                    {isClaimed ? (
                      <button
                        type="button"
                        onClick={() => handleRelease(issue)}
                        disabled={isBusy}
                        className="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary disabled:opacity-50"
                      >
                        {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Unlock className="w-3.5 h-3.5" />}
                        {t('taskMarket.release')}
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => handleClaim(issue)}
                        disabled={isBusy}
                        className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                      >
                        {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Hand className="w-3.5 h-3.5" />}
                        {t('taskMarket.claim')}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function CreateIssueForm({
  missions,
  onSubmit,
  onCancel,
}: {
  missions: { id: string; title: string }[]
  onSubmit: (data: {
    missionId: string
    title: string
    description: string
    riskLevel: RiskLevel
    parallelMode: ParallelMode
  }) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const defaultMission = missions[0]?.id ?? ''
  const [missionId, setMissionId] = useState(defaultMission)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [riskLevel, setRiskLevel] = useState<RiskLevel>('medium')
  const [parallelMode, setParallelMode] = useState<ParallelMode>('cooperative')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || !missionId) return
    onSubmit({ missionId, title: title.trim(), description: description.trim(), riskLevel, parallelMode })
  }

  if (missions.length === 0) {
    return (
      <div className="mb-4 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm text-warning">
        {t('taskMarket.mission')} —
      </div>
    )
  }

  return (
    <form onSubmit={handleSubmit} className="mb-4 rounded-lg border border-border bg-panel p-4 shadow-sm space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-text-secondary">{t('taskMarket.mission')}</span>
          <select
            value={missionId}
            onChange={(e) => setMissionId(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {missions.map((m) => (
              <option key={m.id} value={m.id}>
                {m.title}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-text-secondary">{t('taskMarket.title')}</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
            placeholder={t('taskMarket.title')}
          />
        </label>
      </div>
      <label className="block">
        <span className="text-xs text-text-secondary">{t('taskMarket.description')}</span>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40 resize-none"
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-text-secondary">{t('taskMarket.riskLevel')}</span>
          <select
            value={riskLevel}
            onChange={(e) => setRiskLevel(e.target.value as RiskLevel)}
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {(['low', 'medium', 'high', 'critical'] as RiskLevel[]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-text-secondary">{t('taskMarket.parallelMode')}</span>
          <select
            value={parallelMode}
            onChange={(e) => setParallelMode(e.target.value as ParallelMode)}
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {(['exclusive', 'cooperative', 'competitive', 'exploratory'] as ParallelMode[]).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-text hover:bg-bg-tertiary"
        >
          {t('taskMarket.cancel')}
        </button>
        <button
          type="submit"
          disabled={!title.trim()}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {t('taskMarket.submit')}
        </button>
      </div>
    </form>
  )
}
