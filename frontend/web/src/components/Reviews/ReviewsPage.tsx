import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, X, Play, Loader2, ClipboardCheck, FlaskConical } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import { formatDate } from '@/utils/formatDate'
import type { Approval, ValidationRun, ApprovalState } from '@/api/types'

type Tab = 'approvals' | 'validations'

export function ReviewsPage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const setError = useSessionStore((state) => state.setError)
  const [tab, setTab] = useState<Tab>('approvals')
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [validations, setValidations] = useState<ValidationRun[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [actionId, setActionId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Seed from the snapshot, then refresh from the API for a complete list.
  useEffect(() => {
    setApprovals(snapshot?.approvals ?? [])
    setValidations(snapshot?.validation_runs ?? [])
  }, [snapshot])

  useEffect(() => {
    if (!client || !currentSessionId) {
      setApprovals([])
      setValidations([])
      return
    }
    let cancelled = false
    setIsLoading(true)
    const refresh = async () => {
      try {
        const [approvalResp, validationResp] = await Promise.all([
          client.fetchApprovals(currentSessionId, { limit: 50 }),
          client.fetchValidationRuns(currentSessionId, { limit: 50 }),
        ])
        if (!cancelled) {
          setApprovals(approvalResp.approvals)
          setValidations(validationResp.validation_runs)
        }
      } catch (err) {
        if (!cancelled) {
          setError(isApiException(err) ? err.message : String(err))
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    void refresh()
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId, setError])

  const handleResolve = async (approval: Approval, state: 'approved' | 'rejected') => {
    if (!client || !currentSessionId) return
    setActionId(approval.id)
    setNotice(null)
    try {
      const note = window.prompt(t('reviews.notePlaceholder'), '') ?? ''
      const updated = await client.resolveApproval(currentSessionId, approval.id, state, note || undefined)
      setApprovals((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))
      setNotice(state === 'approved' ? t('reviews.approveSuccess') : t('reviews.rejectSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionId(null)
    }
  }

  const handleRunValidation = async (validation: ValidationRun) => {
    if (!client || !currentSessionId) return
    setActionId(validation.id)
    setNotice(null)
    try {
      const run = await client.runValidation(currentSessionId, {
        task_id: validation.task_id,
        argv: validation.command,
        actor: validation.actor,
        cwd: validation.cwd || undefined,
      })
      setValidations((prev) => [run, ...prev])
      setNotice(t('reviews.validationSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionId(null)
    }
  }

  if (!currentSessionId) {
    return (
      <div className="p-6 h-full overflow-y-auto">
        <h1 className="text-xl font-semibold text-text mb-4">{t('nav.reviews')}</h1>
        <p className="text-text-secondary">{t('reviews.empty')}</p>
      </div>
    )
  }

  const pendingApprovals = approvals.filter((a) => a.state === 'waiting')

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-text">{t('nav.reviews')}</h1>
        {isLoading && <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />}
      </div>

      {notice && (
        <div className="mb-4 rounded-md border border-success/30 bg-success/10 px-4 py-2 text-sm text-success">
          {notice}
        </div>
      )}

      {/* Tab switcher */}
      <div className="mb-4 flex gap-1 border-b border-border">
        <TabButton active={tab === 'approvals'} onClick={() => setTab('approvals')} icon={<ClipboardCheck className="w-4 h-4" />} label={t('reviews.approvals')} count={pendingApprovals.length} />
        <TabButton active={tab === 'validations'} onClick={() => setTab('validations')} icon={<FlaskConical className="w-4 h-4" />} label={t('reviews.validationRuns')} count={validations.length} />
      </div>

      {tab === 'approvals' ? (
        <ApprovalList
          approvals={approvals}
          actionId={actionId}
          onResolve={handleResolve}
          emptyText={t('reviews.empty')}
        />
      ) : (
        <ValidationList
          validations={validations}
          actionId={actionId}
          onRun={handleRunValidation}
          emptyText={t('reviews.empty')}
        />
      )}
    </div>
  )
}

function TabButton({ active, onClick, icon, label, count }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string; count: number }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
        active ? 'border-accent text-accent' : 'border-transparent text-text-secondary hover:text-text'
      }`}
    >
      {icon}
      {label}
      <span className="rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary">{count}</span>
    </button>
  )
}

const APPROVAL_STATE_LABEL: Record<ApprovalState, string> = {
  waiting: 'reviews.waiting',
  approved: 'reviews.approved',
  rejected: 'reviews.rejected',
  not_required: 'reviews.notRequired',
}

function ApprovalList({
  approvals,
  actionId,
  onResolve,
  emptyText,
}: {
  approvals: Approval[]
  actionId: string | null
  onResolve: (approval: Approval, state: 'approved' | 'rejected') => void
  emptyText: string
}) {
  const { t } = useTranslation()
  if (approvals.length === 0) {
    return <div className="text-text-secondary">{emptyText}</div>
  }
  return (
    <div className="space-y-3">
      {approvals.map((approval) => {
        const isWaiting = approval.state === 'waiting'
        const isBusy = actionId === approval.id
        return (
          <div key={approval.id} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-text">{approval.title}</div>
                {approval.detail && (
                  <p className="mt-1 text-sm text-text-secondary line-clamp-3">{approval.detail}</p>
                )}
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                  <span>{t('reviews.requester')}: {approval.requester || '—'}</span>
                  <span>{t('reviews.reviewer')}: {approval.reviewer || '—'}</span>
                  <span>{formatDate(approval.created_at)}</span>
                </div>
                {approval.decision_note && (
                  <div className="mt-2 rounded bg-bg-tertiary px-3 py-1.5 text-xs text-text-secondary">
                    {t('reviews.note')}: {approval.decision_note}
                  </div>
                )}
              </div>
              <div className="flex flex-col items-end gap-2">
                <StateBadge stateKey={APPROVAL_STATE_LABEL[approval.state]} state={approval.state} />
                {isWaiting && (
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => onResolve(approval, 'approved')}
                      disabled={isBusy}
                      className="flex items-center gap-1 rounded-md bg-success px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                    >
                      {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                      {t('reviews.approve')}
                    </button>
                    <button
                      type="button"
                      onClick={() => onResolve(approval, 'rejected')}
                      disabled={isBusy}
                      className="flex items-center gap-1 rounded-md border border-danger px-3 py-1.5 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
                    >
                      <X className="w-3.5 h-3.5" />
                      {t('reviews.reject')}
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ValidationList({
  validations,
  actionId,
  onRun,
  emptyText,
}: {
  validations: ValidationRun[]
  actionId: string | null
  onRun: (validation: ValidationRun) => void
  emptyText: string
}) {
  const { t } = useTranslation()
  if (validations.length === 0) {
    return <div className="text-text-secondary">{emptyText}</div>
  }
  return (
    <div className="space-y-3">
      {validations.map((run) => {
        const isBusy = actionId === run.id
        const passed = run.status === 'passed'
        return (
          <div key={run.id} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      passed ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'
                    }`}
                  >
                    {passed ? t('reviews.passed') : t('reviews.failed')}
                  </span>
                  <span className="text-xs text-text-secondary">{formatDate(run.started_at)}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                  <span>{t('reviews.actor')}: {run.actor || '—'}</span>
                  <span>{t('reviews.exitCode')}: {run.exit_code}</span>
                </div>
                <div className="mt-2 font-mono text-xs text-text-secondary break-all">
                  {t('reviews.command')}: {run.command.join(' ')}
                </div>
                {run.output && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-accent">{t('reviews.output')}</summary>
                    <pre className="mt-1 max-h-48 overflow-auto rounded bg-bg-tertiary p-2 text-xs text-text-secondary whitespace-pre-wrap">{run.output}</pre>
                  </details>
                )}
              </div>
              <button
                type="button"
                onClick={() => onRun(run)}
                disabled={isBusy}
                className="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary disabled:opacity-50"
              >
                {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                {t('reviews.runValidation')}
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function StateBadge({ stateKey, state }: { stateKey: string; state: ApprovalState }) {
  const { t } = useTranslation()
  const color =
    state === 'approved'
      ? 'bg-success/15 text-success'
      : state === 'rejected'
        ? 'bg-danger/15 text-danger'
        : state === 'waiting'
          ? 'bg-warning/15 text-warning'
          : 'bg-bg-tertiary text-text-secondary'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${color}`}>{t(stateKey)}</span>
}
