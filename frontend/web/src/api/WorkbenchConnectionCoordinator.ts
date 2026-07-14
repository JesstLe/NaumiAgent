import { WorkbenchApiClient, type TokenProvider } from './WorkbenchApiClient'
import type { DaemonStatusResponse, Event, WorkbenchSnapshot } from './types'

export interface ConnectionCoordinatorOptions {
  /** Initial base URL; port range scan is used when this fails. */
  initialBaseUrl?: string
  /** Inclusive port range to scan when the initial URL is unreachable. */
  portRange?: [number, number]
  /** How often to poll /workbench/daemon/status while connected. */
  pollIntervalMs?: number
  /** Optional initial listener for workbench events. */
  onEvent?: (event: Event) => void
  /** Optional listener for full snapshot updates pushed over the event stream. */
  onSnapshot?: (snapshot: WorkbenchSnapshot) => void
}

export interface ConnectionStatus {
  isConnected: boolean
  daemon: DaemonStatusResponse | null
  error: string | null
}

type StatusListener = (status: ConnectionStatus) => void

interface WebSocketEnvelope {
  type: string
  version?: number
  payload?: unknown
  message?: string
  count?: number
  session_id?: string
}

export class WorkbenchConnectionCoordinator {
  private apiClient: WorkbenchApiClient | null = null
  private currentBaseUrl: string | null = null
  private currentSessionId: string | null = null
  private daemon: DaemonStatusResponse | null = null
  private lastError: string | null = null
  private ws: WebSocket | null = null
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private refreshTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private isPolling = false
  private openingSessionId: string | null = null
  private statusListeners: StatusListener[] = []
  private eventListeners: Array<(event: Event) => void> = []
  private snapshotListeners: Array<(snapshot: WorkbenchSnapshot) => void> = []
  private options: Required<Pick<ConnectionCoordinatorOptions, 'initialBaseUrl' | 'portRange' | 'pollIntervalMs'>>

  constructor(
    private tokenProvider: TokenProvider,
    options: ConnectionCoordinatorOptions = {},
  ) {
    this.options = {
      initialBaseUrl: options.initialBaseUrl ?? 'http://127.0.0.1:8765/api/v1',
      portRange: options.portRange ?? [8765, 8799],
      pollIntervalMs: options.pollIntervalMs ?? 5000,
    }
    if (options.onEvent) {
      this.eventListeners.push(options.onEvent)
    }
    if (options.onSnapshot) {
      this.snapshotListeners.push(options.onSnapshot)
    }
  }

  get client(): WorkbenchApiClient | null {
    return this.apiClient
  }

  get baseUrl(): string | null {
    return this.currentBaseUrl
  }

  get sessionId(): string | null {
    return this.currentSessionId
  }

  get isConnected(): boolean {
    return this.apiClient !== null && this.lastError === null
  }

  subscribeToStatus(listener: StatusListener): () => void {
    this.statusListeners.push(listener)
    listener(this.getStatus())
    return () => {
      this.statusListeners = this.statusListeners.filter((l) => l !== listener)
    }
  }

  subscribeToEvents(listener: (event: Event) => void): () => void {
    this.eventListeners.push(listener)
    return () => {
      this.eventListeners = this.eventListeners.filter((l) => l !== listener)
    }
  }

  subscribeToSnapshots(listener: (snapshot: WorkbenchSnapshot) => void): () => void {
    this.snapshotListeners.push(listener)
    return () => {
      this.snapshotListeners = this.snapshotListeners.filter((l) => l !== listener)
    }
  }

  private emitStatus() {
    const status = this.getStatus()
    for (const listener of this.statusListeners) {
      try {
        listener(status)
      } catch (error) {
        console.error('Status listener error', error)
      }
    }
  }

  private emitEvent(event: Event) {
    for (const listener of this.eventListeners) {
      try {
        listener(event)
      } catch (error) {
        console.error('Event listener error', error)
      }
    }
  }

  private emitSnapshot(snapshot: WorkbenchSnapshot) {
    for (const listener of this.snapshotListeners) {
      try {
        listener(snapshot)
      } catch (error) {
        console.error('Snapshot listener error', error)
      }
    }
  }

  private getStatus(): ConnectionStatus {
    return {
      isConnected: this.isConnected,
      daemon: this.daemon,
      error: this.lastError,
    }
  }

  /**
   * Try to connect to the workbench daemon.
   * If a baseUrl is provided, only that URL is tried; otherwise the configured
   * port range is scanned.
   */
  async connect(baseUrl?: string): Promise<WorkbenchApiClient> {
    if (this.apiClient) {
      return this.apiClient
    }

    const candidates = baseUrl ? [baseUrl] : this.buildCandidateUrls()
    let lastError: unknown

    for (const url of candidates) {
      try {
        const client = new WorkbenchApiClient(url, this.tokenProvider)
        const status = await client.fetchDaemonStatus()
        this.apiClient = client
        this.currentBaseUrl = url
        this.daemon = status
        this.lastError = null
        this.startPolling()
        this.emitStatus()
        return client
      } catch (error) {
        lastError = error
      }
    }

    const message = lastError instanceof Error ? lastError.message : '无法连接到后端服务'
    this.lastError = message
    this.daemon = null
    this.emitStatus()
    throw new Error(message)
  }

  /**
   * Disconnect from the daemon and close any active WebSocket.
   */
  disconnect(): void {
    this.stopPolling()
    this.stopRefreshLoop()
    this.closeWebSocket()
    this.apiClient = null
    this.currentBaseUrl = null
    this.daemon = null
    this.currentSessionId = null
    this.openingSessionId = null
    this.lastError = null
    this.emitStatus()
  }

  /**
   * Load a session snapshot and subscribe to its event stream.
   */
  async selectSession(sessionId: string): Promise<WorkbenchSnapshot> {
    if (!this.apiClient) {
      throw new Error('未连接到后端服务')
    }
    const snapshot = await this.apiClient.fetchSnapshot(sessionId)
    this.currentSessionId = sessionId
    await this.openEventStream(sessionId)
    return snapshot
  }

  /**
   * Try to bootstrap the workbench by selecting the first available session.
   */
  async bootstrap(): Promise<{ sessionId: string | null; snapshot: WorkbenchSnapshot | null }> {
    if (!this.apiClient) {
      throw new Error('未连接到后端服务')
    }
    const bootstrap = await this.apiClient.fetchBootstrap()
    const payload = bootstrap as {
      selected_session_id?: string | null
      snapshot?: WorkbenchSnapshot | null
    }
    const sessionId = payload.selected_session_id ?? null
    if (sessionId) {
      this.currentSessionId = sessionId
      await this.openEventStream(sessionId)
    }
    return { sessionId, snapshot: payload.snapshot ?? null }
  }

  /**
   * Request a fresh batch of events from the backend event stream.
   */
  refreshEvents(limit = 50): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return
    }
    this.ws.send(JSON.stringify({ type: 'refresh', limit }))
  }

  private buildCandidateUrls(): string[] {
    const [start, end] = this.options.portRange
    const urls: string[] = []
    for (let port = start; port <= end; port++) {
      urls.push(`http://127.0.0.1:${port}/api/v1`)
    }
    return urls
  }

  private startPolling(): void {
    this.stopPolling()
    const run = async () => {
      if (!this.apiClient || this.isPolling) return
      this.isPolling = true
      try {
        this.daemon = await this.apiClient.fetchDaemonStatus()
        this.lastError = null
      } catch (error) {
        this.lastError = error instanceof Error ? error.message : '后端连接异常'
        this.daemon = null
      } finally {
        this.isPolling = false
      }
      this.emitStatus()
    }
    run().catch((error) => console.error('健康检查失败', error))
    this.pollTimer = setInterval(() => {
      run().catch((error) => console.error('健康检查失败', error))
    }, this.options.pollIntervalMs)
  }

  private stopPolling(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
  }

  private startRefreshLoop(): void {
    this.stopRefreshLoop()
    this.refreshTimer = setInterval(() => {
      this.refreshEvents()
    }, this.options.pollIntervalMs)
  }

  private stopRefreshLoop(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer)
      this.refreshTimer = null
    }
  }

  private async openEventStream(sessionId: string): Promise<void> {
    // Guard against concurrent session switches leaving stale sockets.
    this.openingSessionId = sessionId
    this.closeWebSocket()

    if (this.openingSessionId !== sessionId) {
      return
    }

    const template = this.daemon?.event_stream_url_template
    if (!template) {
      console.warn('缺少事件流 URL 模板，跳过 WebSocket 连接')
      return
    }

    const wsUrl = await this.buildEventStreamUrl(template, sessionId)
    try {
      const ws = new WebSocket(wsUrl)
      ws.onopen = () => {
        console.info('事件流已连接', wsUrl)
        this.startRefreshLoop()
        this.refreshEvents()
      }
      ws.onmessage = (message) => {
        this.handleEnvelope(JSON.parse(message.data) as WebSocketEnvelope)
      }
      ws.onerror = (error) => {
        console.error('事件流错误', error)
      }
      ws.onclose = () => {
        this.stopRefreshLoop()
        if (this.currentSessionId === sessionId) {
          this.scheduleReconnect(sessionId)
        }
      }
      this.ws = ws
    } catch (error) {
      console.error('无法打开事件流', error)
    }
  }

  private handleEnvelope(envelope: WebSocketEnvelope) {
    switch (envelope.type) {
      case 'workbench/event': {
        this.emitEvent(envelope.payload as Event)
        break
      }
      case 'workbench/snapshot': {
        this.emitSnapshot(envelope.payload as WorkbenchSnapshot)
        break
      }
      case 'error': {
        console.warn('事件流后端错误', envelope.message)
        break
      }
      case 'connected':
      case 'refresh_complete':
        break
      default: {
        console.warn('未知事件流消息类型', envelope.type)
      }
    }
  }

  private closeWebSocket(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      this.ws.onclose = null
      this.ws.close()
      this.ws = null
    }
  }

  private scheduleReconnect(sessionId: string): void {
    if (this.reconnectTimer) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (this.currentSessionId === sessionId) {
        this.openEventStream(sessionId).catch((error) => console.error('重连事件流失败', error))
      }
    }, this.options.pollIntervalMs)
  }

  private async buildEventStreamUrl(template: string, sessionId: string): Promise<string> {
    const url = new URL(template.replace('{session_id}', encodeURIComponent(sessionId)))
    try {
      const token = await this.tokenProvider()
      if (token) {
        url.searchParams.set('api_key', token)
      }
    } catch {
      // ignore
    }
    return url.toString()
  }
}
