import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2, Filter } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import { formatDate } from '@/utils/formatDate'
import type { Event, EventSeverity } from '@/api/types'

const SEVERITY_COLOR: Record<EventSeverity, string> = {
  info: 'bg-info/15 text-info',
  warning: 'bg-warning/15 text-warning',
  error: 'bg-danger/15 text-danger',
  critical: 'bg-danger/20 text-danger border border-danger/30',
}

const SEVERITY_DOT: Record<EventSeverity, string> = {
  info: 'bg-info',
  warning: 'bg-warning',
  error: 'bg-danger',
  critical: 'bg-danger',
}

export function TimelinePage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const setError = useSessionStore((state) => state.setError)
  const [events, setEvents] = useState<Event[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [severityFilter, setSeverityFilter] = useState<string>('all')

  // Seed from snapshot, then refresh from the API.
  useEffect(() => {
    setEvents(snapshot?.events ?? [])
  }, [snapshot])

  useEffect(() => {
    if (!client || !currentSessionId) {
      setEvents([])
      return
    }
    let cancelled = false
    setIsLoading(true)
    client
      .fetchEvents(currentSessionId, { limit: 200 })
      .then((resp) => {
        if (!cancelled) setEvents(resp.events)
      })
      .catch((err) => {
        if (!cancelled) setError(isApiException(err) ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId, setError])

  // Build the list of distinct event types from the loaded events.
  const eventTypes = useMemo(() => {
    const set = new Set<string>()
    for (const event of events) set.add(event.type)
    return Array.from(set).sort()
  }, [events])

  const filtered = useMemo(() => {
    return events.filter((event) => {
      if (typeFilter !== 'all' && event.type !== typeFilter) return false
      if (severityFilter !== 'all' && event.severity !== severityFilter) return false
      return true
    })
  }, [events, typeFilter, severityFilter])

  if (!currentSessionId) {
    return (
      <div className="p-6 h-full overflow-y-auto">
        <h1 className="text-xl font-semibold text-text mb-4">{t('nav.timeline')}</h1>
        <p className="text-text-secondary">{t('timeline.empty')}</p>
      </div>
    )
  }

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-text">{t('nav.timeline')}</h1>
        {isLoading && <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />}
      </div>

      {/* Filter bar */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <Filter className="w-4 h-4" />
        </div>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
        >
          <option value="all">{t('timeline.allTypes')}</option>
          {eventTypes.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
        <select
          value={severityFilter}
          onChange={(e) => setSeverityFilter(e.target.value)}
          className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
        >
          <option value="all">{t('timeline.allSeverities')}</option>
          {(['info', 'warning', 'error', 'critical'] as EventSeverity[]).map((sev) => (
            <option key={sev} value={sev}>
              {t(`timeline.severity.${sev}`)}
            </option>
          ))}
        </select>
        <span className="text-sm text-text-secondary">
          {filtered.length} / {events.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="text-text-secondary">{t('timeline.empty')}</div>
      ) : (
        <ol className="relative border-l border-border ml-2 space-y-0">
          {filtered.map((event) => (
            <li key={event.id} className="ml-4 pb-4">
              <span
                className={`absolute -left-[7px] mt-1.5 h-3 w-3 rounded-full ring-2 ring-bg ${SEVERITY_DOT[event.severity]}`}
              />
              <div className="rounded-lg border border-border bg-panel p-3 shadow-sm">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${SEVERITY_COLOR[event.severity]}`}>
                    {t(`timeline.severity.${event.severity}`)}
                  </span>
                  <span className="font-mono text-sm font-medium text-text">{event.type}</span>
                  <span className="text-xs text-text-secondary">{formatDate(event.timestamp)}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                  <span>{t('timeline.actor')}: {event.actor || '—'}</span>
                  {event.subject_id && <span>{t('timeline.subject')}: {event.subject_id}</span>}
                  {event.correlation_id && (
                    <span className="font-mono">{t('timeline.correlationId')}: {event.correlation_id}</span>
                  )}
                </div>
                {Object.keys(event.payload).length > 0 && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-accent">payload</summary>
                    <pre className="mt-1 max-h-40 overflow-auto rounded bg-bg-tertiary p-2 text-xs text-text-secondary whitespace-pre-wrap">
                      {JSON.stringify(event.payload, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
