import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import {
  ArrowUp,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  CopyPlus,
  File,
  FolderClosed,
  FolderOpen,
  GitBranch,
  GitPullRequest,
  Globe2,
  HelpCircle,
  Loader2,
  Lightbulb,
  MoreHorizontal,
  Maximize2,
  Minimize2,
  PanelBottom,
  PanelLeft,
  PanelRight,
  Plus,
  Pin,
  PinOff,
  Archive,
  Pencil,
  Plug,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  SquarePen,
  Terminal,
  Workflow,
  X,
} from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { usePlatform } from '@naumi/shared/platform'
import {
  readPreference,
  safeWebUrl,
  savePreference,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import { errorText } from '@naumi/shared/hooks/useWorkspaceController'
import { runsByUserMessage } from '@naumi/shared/api/activity'
import './web2.css'
import { MenuBar } from './MenuBar'
import { SettingsPage } from './SettingsPage'
import { TodoPanel } from './TodoPanel'
import { GoalPanel } from './GoalPanel'
import { DiffPanel } from './DiffPanel'
import { ThinkingState } from './beautiful/ThinkingState'
import { MessageContent } from './rich/MessageContent'
import { WorkspaceFileTree } from './community/WorkspaceFileTree'
import { AiMessage } from './community/AiMessage'
import { SelectionActions } from './beautiful/SelectionActions'
import { ContextCards } from './beautiful/ContextCards'
import { Flowchart } from './beautiful/Flowchart'
import { InsightCards } from './beautiful/InsightCards'
import './beautiful/upstream.generated.css'
import './beautiful/beautiful.css'
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import { useCommandCompletion } from '@naumi/shared/hooks/useCommandCompletion'
import type {
  GitBranchesResponse,
  ScheduleJob,
  SkillExtensionsResponse,
} from '@naumi/shared/api/types'

type Panel =
  | 'home'
  | 'review'
  | 'files'
  | 'browser'
  | 'tools'
  | 'tasks'
  | 'context'
  | 'flow'
  | 'insights'
  | 'schedules'
  | 'plugins'
const panelNames: Record<Panel, string> = {
  home: '工作区',
  review: '审查',
  files: '文件',
  browser: '浏览器',
  tools: '工具与扩展',
  tasks: '任务',
  context: '上下文',
  flow: '依赖',
  insights: '洞察',
  schedules: '定时任务',
  plugins: '插件',
}

const MIN_LEFT_WIDTH = 200
const MAX_LEFT_WIDTH = 420
const MIN_RIGHT_WIDTH = 280
const MAX_RIGHT_WIDTH = 720

function defaultLeftWidth() {
  return window.innerWidth <= 1100 ? 215 : 240
}

function minimumChatWidth() {
  return window.innerWidth <= 1100 ? 330 : 420
}

function defaultRightWidth(leftWidth = defaultLeftWidth()) {
  const available = window.innerWidth - leftWidth - minimumChatWidth() - 6
  return Math.max(MIN_RIGHT_WIDTH, Math.min(480, available))
}

function storedWidth(key: string, fallback: number) {
  const value = Number(readPreference(key, String(fallback)))
  return Number.isFinite(value) ? value : fallback
}

function Logo({ className = '' }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 56 56"
      fill="none"
      aria-label="NaumiAgent"
      role="img"
    >
      <path
        d="M20 9c4-5 12-5 16 0 7-1 13 5 12 12 5 4 5 12 0 16 1 7-5 13-12 12-4 5-12 5-16 0-7 1-13-5-12-12-5-4-5-12 0-16C7 14 13 8 20 9Z"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      <path
        d="m19 22 5 7-5 7m12 0h8"
        stroke="currentColor"
        strokeWidth="2.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconButton({
  label,
  children,
  onClick,
  active,
  disabled,
}: {
  label: string
  children: ReactNode
  onClick: () => void
  active?: boolean
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      className={`w2-icon ${active ? 'is-active' : ''}`}
      title={label}
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}
function SchedulePanel() {
  const w = useWorkspace()
  const [jobs, setJobs] = useState<ScheduleJob[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [kind, setKind] = useState<'once' | 'cron'>('once')
  const [expression, setExpression] = useState('')
  const [prompt, setPrompt] = useState('')
  const load = async () => {
    setLoading(true)
    try {
      setJobs((await w.api.schedules()).schedules)
    } catch (error) {
      w.setError(errorText(error))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void load() }, [])
  const control = async (id: string, action: 'pause' | 'resume' | 'cancel') => {
    try {
      const updated = await w.api.controlSchedule(id, action)
      setJobs(previous => previous.map(job => job.id === id ? updated : job))
    } catch (error) {
      w.setError(errorText(error))
    }
  }
  return <div className="w2-resource-panel">
    <div className="w2-section-heading"><span>定时任务 <small>{jobs.length}</small></span><button disabled={loading} onClick={() => void load()}>刷新</button></div>
    <form className="w2-resource-form" onSubmit={async event => {
      event.preventDefault(); setSaving(true)
      try {
        const created = await w.api.createSchedule({ kind, expression, prompt })
        setJobs(previous => [created, ...previous]); setExpression(''); setPrompt('')
      } catch (error) { w.setError(errorText(error)) } finally { setSaving(false) }
    }}>
      <div><select aria-label="定时类型" value={kind} onChange={event => setKind(event.target.value as 'once' | 'cron')}><option value="once">单次</option><option value="cron">周期</option></select>
        <input aria-label="执行时间" required value={expression} onChange={event => setExpression(event.target.value)} placeholder={kind === 'once' ? '2026-09-12T09:00:00+08:00' : '*/15 * * * *'} /></div>
      <textarea aria-label="提醒内容" required value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="触发时要继续处理什么？" />
      <button className="w2-primary" disabled={saving || !expression.trim() || !prompt.trim()}>{saving ? '创建中…' : '创建定时任务'}</button>
    </form>
    {!jobs.length && <p className="w2-muted">{loading ? '正在读取定时任务…' : '暂无定时任务'}</p>}
    {jobs.map(job => <article className="w2-resource-card" key={job.id}>
      <header><Clock3 size={15} /><strong>{job.kind === 'cron' ? '周期任务' : '单次任务'}</strong><span>{({ active: '启用', paused: '暂停', cancelled: '已取消', completed: '已完成' } as Record<string, string>)[job.status]}</span></header>
      <p>{job.prompt}</p><code>{job.expression}</code>{job.next_fire_at && <small>下次执行 {new Date(job.next_fire_at).toLocaleString('zh-CN')}</small>}
      <div>{job.status === 'active' && <button onClick={() => void control(job.id, 'pause')}>暂停</button>}{job.status === 'paused' && <button onClick={() => void control(job.id, 'resume')}>恢复</button>}{!['cancelled', 'completed'].includes(job.status) && <button onClick={() => void control(job.id, 'cancel')}>取消</button>}</div>
    </article>)}
  </div>
}

function PluginPanel() {
  const w = useWorkspace()
  const [data, setData] = useState<SkillExtensionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const load = async () => {
    setLoading(true)
    try { setData(await w.api.skillExtensions()) }
    catch (error) { w.setError(errorText(error)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  return <div className="w2-resource-panel">
    <div className="w2-section-heading"><span>插件与 Skills {data && <small>{data.summary.selected} 个生效</small>}</span><button disabled={loading} onClick={() => void load()}>刷新</button></div>
    {!data?.skills.length && <p className="w2-muted">{loading ? '正在扫描插件…' : '没有发现 Skill 插件'}</p>}
    {data?.skills.map(skill => <article className={`w2-resource-card ${skill.state}`} key={`${skill.source_scope}:${skill.manifest_path}`}>
      <header><Plug size={15} /><strong>{skill.name}</strong><span>{skill.state === 'selected' ? '已启用' : skill.state === 'shadowed' ? '已被覆盖' : '无效'}</span></header>
      <p>{skill.manifest_path}</p><small>{skill.source_scope} · 优先级 {skill.source_priority + 1}</small>
    </article>)}
    {!!data?.sources.length && <details className="w2-plugin-sources"><summary>插件来源</summary>{data.sources.map(source => <p key={source.path}>{source.priority + 1}. {source.path} · {source.available ? '可用' : '目录不存在'}</p>)}</details>}
  </div>
}

export function Web2() {
  const w = useWorkspace()
  const platform = usePlatform()
  const completion = useCommandCompletion(w.commands, w.draft, w.setDraft)
  const [sidebar, setSidebar] = useState(
    () => readPreference('sidebar', String(window.innerWidth > 580)) === 'true',
  )
  const [right, setRight] = useState(
    () => readPreference('right', 'true') === 'true',
  )
  const [terminal, setTerminal] = useState(
    () => readPreference('terminal', 'true') === 'true',
  )
  const [leftWidth, setLeftWidth] = useState(() =>
    Math.min(MAX_LEFT_WIDTH, Math.max(MIN_LEFT_WIDTH, storedWidth('sidebar-width', defaultLeftWidth()))),
  )
  const [rightWidth, setRightWidth] = useState(() =>
    Math.min(MAX_RIGHT_WIDTH, Math.max(MIN_RIGHT_WIDTH, storedWidth('right-width', defaultRightWidth()))),
  )
  const [resizing, setResizing] = useState<'left' | 'right' | null>(null)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [panel, setPanel] = useState<Panel>('home')
  const [expanded, setExpanded] = useState(true)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')
  const [toolQuery, setToolQuery] = useState('')
  const [browserUrl, setBrowserUrl] = useState('')
  const [dialog, setDialog] = useState<'settings' | 'help' | 'delete' | 'rename' | null>(
    null,
  )
  const [renameTitle, setRenameTitle] = useState('')
  const [renameSessionId, setRenameSessionId] = useState('')
  const [sessionMenu, setSessionMenu] = useState<string | null>(null)
  const [workspaceMenu, setWorkspaceMenu] = useState(false)
  const [gitState, setGitState] = useState<GitBranchesResponse | null>(null)
  const [workspaceSwitching, setWorkspaceSwitching] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const summaryRef = useRef<HTMLDivElement>(null)
  const resizeRef = useRef<{
    side: 'left' | 'right'
    pointerId: number
    startX: number
    startWidth: number
    width: number
  } | null>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const followOutput = useRef(true)
  const workspace = w.daemon?.workspace_name || 'NaumiAgent'
  const current = w.sessions.find((item) => item.id === w.sessionId)
  const messages = w.messages.filter(
    (item) => ['user', 'assistant'].includes(item.role) && item.content,
  )
  const latestUserIndex = messages.reduce(
    (latest, item, index) => item.role === 'user' ? index : latest,
    -1,
  )
  const messageRuns = useMemo(
    () => runsByUserMessage(messages, w.runs),
    [messages, w.runs],
  )
  const liveRunId = String(
    w.liveEvents.at(-1)?.run_id || w.liveEvents.at(-1)?.data.run_id || '',
  )
  const composerLocked = w.busy || w.uploading || w.loading || w.connecting
  const uploadLocked = w.busy || w.uploading || !w.daemon
  const currentWriteLocked = (id: string) =>
    w.mutating || (w.busy && w.runningSessionId === id)
  const branch = w.diff?.branch || w.snapshot?.worktrees[0]?.branch
  const filteredSessions = w.sessions.filter((item) =>
    (item.title || item.id).toLowerCase().includes(query.trim().toLowerCase()),
  )
  const layoutStyle = {
    '--w2-sidebar-width': `${leftWidth}px`,
    '--w2-right-width': `${rightWidth}px`,
  } as CSSProperties

  const clampPaneWidth = (side: 'left' | 'right', value: number) => {
    if (side === 'left') {
      const rightSpace = right ? rightWidth + 6 : 0
      const available = window.innerWidth - rightSpace - minimumChatWidth()
      return Math.round(Math.min(MAX_LEFT_WIDTH, Math.max(MIN_LEFT_WIDTH, available), Math.max(MIN_LEFT_WIDTH, value)))
    }
    const leftSpace = sidebar ? leftWidth : 0
    const available = window.innerWidth - leftSpace - minimumChatWidth() - 6
    const maximum = Math.max(MIN_RIGHT_WIDTH, Math.min(MAX_RIGHT_WIDTH, available))
    return Math.round(Math.min(maximum, Math.max(MIN_RIGHT_WIDTH, value)))
  }

  const setPaneWidth = (side: 'left' | 'right', value: number, persist = false) => {
    const next = clampPaneWidth(side, value)
    if (side === 'left') setLeftWidth(next)
    else setRightWidth(next)
    if (persist) savePreference(`${side === 'left' ? 'sidebar' : 'right'}-width`, String(next))
    return next
  }

  const startResize = (side: 'left' | 'right') => (event: ReactPointerEvent<HTMLDivElement>) => {
    if (window.matchMedia('(max-width: 820px)').matches) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    const width = side === 'left' ? leftWidth : rightWidth
    resizeRef.current = { side, pointerId: event.pointerId, startX: event.clientX, startWidth: width, width }
    setResizing(side)
  }

  const moveResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = resizeRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const delta = event.clientX - drag.startX
    drag.width = setPaneWidth(drag.side, drag.startWidth + (drag.side === 'left' ? delta : -delta))
  }

  const finishResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = resizeRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    savePreference(`${drag.side === 'left' ? 'sidebar' : 'right'}-width`, String(drag.width))
    resizeRef.current = null
    setResizing(null)
  }

  const resizeWithKeyboard = (side: 'left' | 'right', direction: number, large: boolean) => {
    if (window.matchMedia('(max-width: 820px)').matches) return
    const delta = (large ? 40 : 16) * direction
    const currentWidth = side === 'left' ? leftWidth : rightWidth
    setPaneWidth(side, currentWidth + delta * (side === 'left' ? 1 : -1), true)
  }

  const toggleSidebar = () =>
    setSidebar((value) => {
      savePreference('sidebar', String(!value))
      return !value
    })
  const toggleRight = () =>
    setRight((value) => {
      savePreference('right', String(!value))
      return !value
    })
  const toggleTerminal = () =>
    setTerminal((value) => {
      savePreference('terminal', String(!value))
      return !value
    })
  const panelControls = (
    <div className="w2-panel-controls" role="group" aria-label="面板显示控制">
      <IconButton label="切换右侧面板" active={right} onClick={toggleRight}><PanelRight /></IconButton>
      <IconButton label="切换执行面板" active={terminal} onClick={toggleTerminal}><PanelBottom /></IconButton>
    </div>
  )
  const openPanel = (next: Panel) => {
    setPanel(next)
    setRight(true)
  }
  const newChat = () => {
    void w.select(null)
    textarea.current?.focus()
  }
  const loadGitState = async () => {
    try { setGitState(await w.api.gitBranches()) }
    catch (error) { w.setError(errorText(error)) }
  }
  const openWorkspaceMenu = () => {
    setWorkspaceMenu(value => !value)
    if (!workspaceMenu) void loadGitState()
  }
  const openPath = (mode: 'explorer' | 'terminal') => {
    const path = w.daemon?.workspace_root
    const action = mode === 'explorer' ? platform.openInExplorer : platform.openInTerminal
    if (!path || !action) return
    void action.call(platform, path).catch(error => w.setError(errorText(error)))
  }
  const chooseWorkspace = async () => {
    if (!platform.selectWorkspaceDirectory || !platform.getDaemonLaunchConfig || !platform.startDaemon) return
    const path = await platform.selectWorkspaceDirectory(w.daemon?.workspace_root)
    if (!path) return
    setWorkspaceSwitching(true)
    try {
      const previous = await platform.getDaemonLaunchConfig()
      const config = previous ?? { executable: null, args: [], working_dir: null, port: null, env_vars: {} }
      config.working_dir = path
      await platform.setDaemonLaunchConfig?.(config)
      const status = await platform.startDaemon(config)
      if (!status.running || !status.url) throw new Error(status.last_error || '新工作目录启动失败')
      savePreference('api', status.url)
      window.location.reload()
    } catch (error) {
      w.setError(errorText(error)); setWorkspaceSwitching(false)
    }
  }

  useEffect(() => {
    if (dialog) {
      dialogRef.current?.showModal()
    } else dialogRef.current?.close()
  }, [dialog, platform, w.base])
  useEffect(() => {
    const update = () => setFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', update)
    return () => document.removeEventListener('fullscreenchange', update)
  }, [])
  useEffect(() => {
    const element = textarea.current
    if (element) {
      element.style.height = 'auto'
      element.style.height = `${Math.min(180, Math.max(52, element.scrollHeight))}px`
    }
  }, [w.draft])
  useEffect(() => {
    if (followOutput.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight
  }, [w.messages, w.busy])
  useEffect(() => {
    followOutput.current = true
  }, [w.sessionId])
  useEffect(() => {
    if (!summaryOpen) return
    const closeOutside = (event: PointerEvent) => {
      if (!summaryRef.current?.contains(event.target as Node)) setSummaryOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [summaryOpen])
  useEffect(() => {
    const fitPanes = () => {
      if (window.innerWidth <= 820) return
      const nextRight = clampPaneWidth('right', rightWidth)
      if (nextRight !== rightWidth) setRightWidth(nextRight)
      const nextLeft = clampPaneWidth('left', leftWidth)
      if (nextLeft !== leftWidth) setLeftWidth(nextLeft)
    }
    window.addEventListener('resize', fitPanes)
    return () => window.removeEventListener('resize', fitPanes)
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSearching(false)
        setSummaryOpen(false)
        return
      }
      if (!(event.ctrlKey || event.metaKey) || dialog) return
      const key = event.key.toLowerCase()
      if (key === 'k') {
        event.preventDefault()
        setSearching(true)
        setSidebar(true)
      }
      if (key === 'j') {
        event.preventDefault()
        toggleTerminal()
      }
      if (key === 'b') {
        event.preventDefault()
        toggleSidebar()
      }
      if (key === 'g' && event.shiftKey) {
        event.preventDefault()
        openPanel('review')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div
      className={`web2 ${sidebar ? '' : 'w2-sidebar-hidden'} ${right ? '' : 'w2-right-hidden'} ${resizing ? `w2-resizing-${resizing}` : ''}`}
      style={layoutStyle}
    >
      <SelectionActions focusComposer={() => textarea.current?.focus()} />
      <header className="w2-titlebar">
        <MenuBar menus={[
          { label: '文件', actions: [
            { label: '新建对话', run: newChat },
            { label: '新建侧边聊天', run: () => { void w.createSidebarSession() }, disabled: !w.daemon || w.mutating },
            { label: '打开新的工作目录', run: () => { void chooseWorkspace() }, disabled: !platform.selectWorkspaceDirectory || workspaceSwitching || w.busy },
            { label: '添加文件', run: () => fileInput.current?.click(), disabled: uploadLocked },
            { label: '导出对话', disabled: !messages.length, run: () => {
              const blob = new Blob([messages.map(item => `## ${item.role === 'user' ? '用户' : 'NaumiAgent'}\n\n${item.content}`).join('\n\n')], { type: 'text/markdown;charset=utf-8' })
              const url = URL.createObjectURL(blob)
              const link = document.createElement('a')
              link.href = url; link.download = `naumi-${w.sessionId || 'chat'}.md`; link.click()
              setTimeout(() => URL.revokeObjectURL(url), 1000)
            } },
            { label: '设置', run: () => setDialog('settings') },
            { label: '删除当前会话', disabled: !current || currentWriteLocked(current.id), run: () => setDialog('delete') },
          ] },
          { label: '编辑', actions: [
            { label: '编辑消息', run: () => textarea.current?.focus() },
            { label: '全选消息草稿', disabled: !w.draft, run: () => { textarea.current?.focus(); textarea.current?.select() } },
            { label: '复制消息草稿', disabled: !w.draft, run: () => { void navigator.clipboard.writeText(w.draft).catch(() => w.setError('复制失败，请选中文字复制')) } },
            { label: '搜索会话', shortcut: 'Ctrl+K', run: () => { setSidebar(true); setSearching(true) } },
          ] },
          { label: '视图', actions: [
            { label: sidebar ? '隐藏侧栏' : '显示侧栏', shortcut: 'Ctrl+B', run: toggleSidebar },
            { label: right ? '隐藏右面板' : '显示右面板', run: toggleRight },
            { label: terminal ? '隐藏执行记录' : '显示执行记录', shortcut: 'Ctrl+J', run: toggleTerminal },
            { label: '代码更改', shortcut: 'Ctrl+Shift+G', run: () => openPanel('review') },
            { label: '会话文件', run: () => openPanel('files') },
            { label: '置顶摘要', run: () => setSummaryOpen(true) },
            { label: '上下文快照', run: () => openPanel('context') },
            { label: '任务依赖图', run: () => openPanel('flow') },
            { label: '会话洞察', run: () => openPanel('insights') },
            { label: '定时任务', run: () => openPanel('schedules') },
            { label: '插件', run: () => openPanel('plugins') },
            { label: '任务记录', run: () => openPanel('tasks') },
            { label: fullscreen ? '退出全屏' : '进入全屏', run: () => { void (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => w.setError('浏览器暂不支持全屏')) } },
          ] },
          { label: '帮助', actions: [
            { label: '快捷键', run: () => setDialog('help') },
            { label: '工具与扩展', run: () => openPanel('tools') },
            { label: '重新连接', disabled: w.connecting, run: () => { void w.connect() } },
          ] },
        ]} />
        <IconButton label="切换侧栏" onClick={toggleSidebar}>
          <PanelLeft />
        </IconButton>
        <span>NaumiAgent</span>
        <span className="w2-title-divider">/</span>
        <div className="w2-workspace-switcher">
          <button aria-label="工作目录与运行环境" aria-expanded={workspaceMenu} onClick={openWorkspaceMenu}>{workspace}<ChevronDown size={12} /></button>
          {workspaceMenu && <div className="w2-workspace-menu">
            <header><FolderClosed size={15} /><div><strong>{workspace}</strong><small>{w.daemon?.workspace_root || '未连接工作目录'}</small></div></header>
            <label>运行位置<select aria-label="运行位置" value="local" onChange={() => {}}><option value="local">本地</option><option value="cloud" disabled>云端（尚未连接）</option></select></label>
            {gitState?.available && <label>Git 分支<select aria-label="Git 分支" value={gitState.current} disabled={w.busy || gitState.dirty} onChange={async event => {
              try { setGitState(await w.api.switchGitBranch(event.target.value)); void w.loadDiff() }
              catch (error) { w.setError(errorText(error)) }
            }}>{gitState.branches.map(name => <option value={name} key={name}>{name}</option>)}</select>{gitState.dirty && <small>工作区有未提交修改，暂不能切换</small>}</label>}
            {gitState && !gitState.available && <p>{gitState.error}</p>}
            <div className="w2-workspace-actions">
              <button disabled={!platform.openInExplorer} onClick={() => openPath('explorer')}>资源管理器</button>
              <button disabled={!platform.openInTerminal} onClick={() => openPath('terminal')}>终端</button>
            </div>
            <button className="w2-workspace-open" disabled={!platform.selectWorkspaceDirectory || workspaceSwitching || w.busy} onClick={() => void chooseWorkspace()}>{workspaceSwitching ? '正在切换…' : '打开新的工作目录'}</button>
          </div>}
        </div>
        <div className="w2-title-end">
          <span className={`w2-connection-dot ${w.daemon ? 'online' : ''}`} />
          {w.connecting ? '连接中' : w.daemon ? '本地工作区' : '未连接'}
        </div>
      </header>
      <aside className="w2-sidebar" aria-label="项目侧栏">
        <div className="w2-brand">
          <button onClick={openWorkspaceMenu}>
            <span>NaumiAgent</span>
            <ChevronDown size={14} />
          </button>
          <IconButton
            label="搜索会话"
            onClick={() => setSearching((value) => !value)}
          >
            <Search />
          </IconButton>
        </div>
        <nav className="w2-nav" aria-label="主导航">
          <button onClick={newChat}>
            <SquarePen />
            新对话<span className="w2-nav-hint">＋</span>
          </button>
          <button
            onClick={() => openPanel('tools')}
            className={panel === 'tools' ? 'selected' : ''}
          >
            <Workflow />
            工具与扩展
          </button>
          <button onClick={() => openPanel('schedules')} className={panel === 'schedules' ? 'selected' : ''}><Clock3 />定时任务</button>
          <button onClick={() => openPanel('plugins')} className={panel === 'plugins' ? 'selected' : ''}><Plug />插件</button>
        </nav>
        {searching && (
          <div className="w2-search">
            <Search size={14} />
            <input
              autoFocus
              aria-label="搜索历史会话"
              placeholder="搜索会话…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <IconButton
              label="关闭搜索"
              onClick={() => {
                setSearching(false)
                setQuery('')
              }}
            >
              <X />
            </IconButton>
          </div>
        )}
        <div className="w2-project-label">
          项目
          <IconButton label="新建侧边聊天" disabled={!w.daemon || w.mutating} onClick={() => { void w.createSidebarSession() }}>
            <Plus />
          </IconButton>
        </div>
        <div className="w2-project-scroll">
          <button
            className="w2-project"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
          >
            {expanded ? <FolderOpen /> : <FolderClosed />}
            <span>{workspace}</span>
            {expanded ? <ChevronDown /> : <ChevronRight />}
          </button>
          {expanded && (
            <div className="w2-session-list">
              {filteredSessions.map((session) => (
                <div className={`w2-session-row ${session.id === w.sessionId ? 'selected' : ''}`} key={session.id}>
                  <button className="w2-session-open" aria-current={session.id === w.sessionId ? 'page' : undefined} title={session.title || '新对话'} onClick={() => void w.select(session.id)}>
                    {session.pinned && <Pin size={11} />}
                    <span>{session.title || '新对话'}</span>
                    {w.busy && session.id === w.runningSessionId && <Loader2 className="w2-spin" size={13} />}
                  </button>
                  <IconButton label={`会话操作 ${session.title || '新对话'}`} active={sessionMenu === session.id} onClick={() => setSessionMenu(value => value === session.id ? null : session.id)}><MoreHorizontal /></IconButton>
                  {sessionMenu === session.id && <div className="w2-session-menu">
                    <button disabled={currentWriteLocked(session.id)} onClick={() => { setRenameTitle(session.title || '新对话'); setRenameSessionId(session.id); setDialog('rename'); setSessionMenu(null) }}><Pencil />修改名称</button>
                    <button disabled={w.mutating} onClick={() => { void w.pinSession(session.id, !session.pinned); setSessionMenu(null) }}>{session.pinned ? <PinOff /> : <Pin />}{session.pinned ? '取消置顶' : '置顶'}</button>
                    <button disabled={currentWriteLocked(session.id)} onClick={() => { void w.duplicateSession(session.id); setSessionMenu(null) }}><CopyPlus />复制会话</button>
                    <button disabled={!platform.openInExplorer} onClick={() => { openPath('explorer'); setSessionMenu(null) }}><FolderOpen />在资源管理器中打开</button>
                    <button disabled={!platform.openInTerminal} onClick={() => { openPath('terminal'); setSessionMenu(null) }}><Terminal />在终端中打开</button>
                    <button disabled={currentWriteLocked(session.id)} onClick={() => { void w.archiveSession(session.id); setSessionMenu(null) }}><Archive />归档</button>
                  </div>}
                </div>
              ))}
              {!filteredSessions.length && (
                <div className="w2-sidebar-empty">
                  {query
                    ? '没有匹配的会话'
                    : w.connecting
                      ? '正在加载…'
                      : '暂无历史会话'}
                </div>
              )}
            </div>
          )}
        </div>
        <footer className="w2-sidebar-footer">
          <button onClick={() => setDialog('settings')}>
            <Settings2 />
            设置
          </button>
          <IconButton label="帮助与快捷键" onClick={() => setDialog('help')}>
            <HelpCircle />
          </IconButton>
        </footer>
      </aside>

      <div
        className="w2-pane-resizer w2-sidebar-resizer"
        role="separator"
        aria-label="调整左侧栏宽度"
        aria-orientation="vertical"
        aria-valuemin={MIN_LEFT_WIDTH}
        aria-valuemax={MAX_LEFT_WIDTH}
        aria-valuenow={leftWidth}
        tabIndex={0}
        onPointerDown={startResize('left')}
        onPointerMove={moveResize}
        onPointerUp={finishResize}
        onPointerCancel={finishResize}
        onDoubleClick={() => setPaneWidth('left', defaultLeftWidth(), true)}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
          event.preventDefault()
          resizeWithKeyboard('left', event.key === 'ArrowRight' ? 1 : -1, event.shiftKey)
        }}
      />

      <main className={`w2-main ${terminal ? 'has-terminal' : ''}`}>
        <div className="w2-workspace">
          <section className="w2-chat" aria-label="对话">
            <div className="w2-chat-heading">
              <span>{current?.title || '新对话'}</span>
              <div className="w2-summary-anchor" ref={summaryRef}>
                <button
                  type="button"
                  className="w2-summary-trigger"
                  aria-label="置顶摘要"
                  aria-expanded={summaryOpen}
                  onClick={() => setSummaryOpen(value => !value)}
                >
                  <Pin />
                  <span>置顶摘要</span>
                </button>
                {summaryOpen && (
                  <div className="w2-summary-popover" role="region" aria-label="置顶摘要">
                    <header>
                      <strong>置顶摘要</strong>
                      <IconButton label="关闭置顶摘要" onClick={() => setSummaryOpen(false)}><X /></IconButton>
                    </header>
                    <section aria-label="待办摘要">
                      <TodoPanel key={w.sessionId || 'new'} />
                    </section>
                    <hr />
                    <section aria-label="目标摘要">
                      <GoalPanel />
                    </section>
                  </div>
                )}
              </div>
              <div className="w2-mobile-tools"><IconButton label="打开文件面板" onClick={() => openPanel('files')}><FolderClosed /></IconButton></div>
              <div className={`w2-chat-panel-controls ${right ? 'right-open' : ''}`}>{panelControls}</div>
            </div>
            <div
              className="w2-conversation"
              ref={scroll}
              onScroll={() => {
                const el = scroll.current
                if (el)
                  followOutput.current =
                    el.scrollHeight - el.scrollTop - el.clientHeight < 80
              }}
            >
              {!messages.length && !w.loading && !w.busy && !w.runs.length && !w.liveEvents.length ? (
                <div className="w2-welcome">
                  <Logo className="w2-welcome-logo" />
                  <h1>
                    你想让我们在 <span>{workspace}</span> 中构建什么？
                  </h1>
                </div>
              ) : (
                <div className="w2-message-list">
                  {w.loading && <p className="w2-muted">正在加载会话…</p>}
                  {messages.map((message, index) => {
                    const run = message.role === 'user' ? messageRuns.get(message.id) : undefined
                    const live = message.role === 'user'
                      && index === latestUserIndex
                      && (w.busy || w.liveEvents.length > 0)
                      && (!liveRunId || !run || liveRunId === run.id)
                    return <Fragment key={message.id}>
                      <AiMessage
                        from={message.role as 'user' | 'assistant'}
                        timestamp={message.timestamp}
                        seed={`assistant:${w.sessionId || 'new'}`}
                        copyText={message.content}
                        onCopyError={() => w.setError('复制失败，请选中文字复制')}
                      >
                        <MessageContent content={message.content} plain={message.role === 'user'} />
                      </AiMessage>
                      {message.role === 'user' && (run || live) && (
                        <ThinkingState run={run} live={live} objective={message.content} />
                      )}
                    </Fragment>
                  })}
                  {!messages.some(message => message.role === 'user') && (w.runs[0] || w.busy || w.liveEvents.length > 0) && (
                    <ThinkingState
                      run={w.runs[0]}
                      live={w.busy || (!w.runs[0] && w.liveEvents.length > 0)}
                    />
                  )}
                </div>
              )}
            </div>
            <div className="w2-composer-wrap">
              {w.error && (
                <div className="w2-error" role="alert">
                  <span>{w.error}</span>
                  <div>
                    {!w.daemon && (
                      <button
                        disabled={w.connecting}
                        onClick={() => void w.connect()}
                      >
                        重新连接
                      </button>
                    )}
                    <IconButton label="关闭提示" onClick={() => w.setError('')}>
                      <X />
                    </IconButton>
                  </div>
                </div>
              )}
              {w.permissions.map((permission) => (
                <div className="w2-permission" key={permission.callId}>
                  <strong>{permission.name} 需要确认</strong>
                  <p>{permission.reason}</p>
                  <button onClick={() => void w.resolve(permission, 'deny')}>
                    拒绝
                  </button>
                  <button onClick={() => void w.resolve(permission, 'allow')}>
                    允许本次
                  </button>
                </div>
              ))}
              <div className="w2-context-bar">
                <span title={w.daemon?.workspace_root}>
                  <FolderClosed />
                  {workspace}
                </span>
                <span>
                  <PanelBottom />
                  本地
                </span>
                {branch && (
                  <span>
                    <GitBranch />
                    {branch}
                  </span>
                )}
              </div>
              <div
                className="w2-composer"
                onDragOver={(event) => {
                  event.preventDefault()
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  if (!uploadLocked) void w.upload(event.dataTransfer.files)
                }}
              >
                <textarea
                  ref={textarea}
                  aria-label="消息"
                  aria-controls={completion.items.length ? 'w2-command-list' : undefined}
                  aria-activedescendant={completion.items.length ? `w2-command-${completion.index}` : undefined}
                  placeholder="描述任务，或输入 / 使用命令"
                  value={w.draft}
                  disabled={w.loading || w.connecting}
                  onChange={(event) => w.setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (completion.onKeyDown(event)) return
                    if (
                      event.key === 'Enter' &&
                      (w.sendKey === 'enter' || event.ctrlKey || event.metaKey) &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing &&
                      event.keyCode !== 229
                    ) {
                      event.preventDefault()
                      void w.send()
                    }
                  }}
                />
                {!!completion.items.length && <div className="w2-command-list" role="listbox" id="w2-command-list" aria-label="斜杠命令">
                  {completion.items.map((command, index) => <button type="button" role="option" tabIndex={-1} id={`w2-command-${index}`} key={command.command} aria-selected={index === completion.index}
                    onMouseDown={event => event.preventDefault()} onClick={() => { completion.pick(command); textarea.current?.focus() }}>
                    <strong>{command.command}</strong><span>{command.description}</span><small>{command.arguments.syntax}</small>
                  </button>)}
                </div>}
                {w.draft.startsWith('/') && w.commandsError && <p className="w2-muted" role="status">{w.commandsError}</p>}
                {!!w.selectedSources.length && (
                  <div className="w2-attachments">
                    {w.sources
                      .filter((item) => w.selectedSources.includes(item.id))
                      .map((source) => (
                        <span key={source.id}>
                          <File size={13} />
                          {source.title}
                          <IconButton
                            label={`移除附件 ${source.title}`}
                            onClick={() =>
                              w.setSelectedSources(
                                w.selectedSources.filter(
                                  (id) => id !== source.id,
                                ),
                              )
                            }
                          >
                            <X />
                          </IconButton>
                        </span>
                      ))}
                  </div>
                )}
                <div className="w2-composer-actions">
                  <input
                    hidden
                    ref={fileInput}
                    type="file"
                    multiple
                    onChange={(event) => {
                      if (event.target.files) void w.upload(event.target.files)
                      event.target.value = ''
                    }}
                  />
                  <IconButton
                    label="添加附件"
                    disabled={uploadLocked}
                    onClick={() => fileInput.current?.click()}
                  >
                    {w.uploading ? <Loader2 className="w2-spin" /> : <Plus />}
                  </IconButton>
                  <label className="w2-mode" title="执行权限">
                    <ShieldCheck size={14} />
                    <select
                      aria-label="执行模式"
                      value={w.mode}
                      disabled={w.uploading}
                      onChange={(event) =>
                        w.setMode(event.target.value as typeof w.mode)
                      }
                    >
                      <option value="default">默认权限</option>
                      <option value="plan">计划模式</option>
                      <option value="bypass">跳过审批</option>
                    </select>
                  </label>
                  <div className="w2-model">
                    <select
                      aria-label="模型"
                      value={w.model}
                      disabled={!w.daemon || w.uploading || (w.busy && w.sessionId === w.runningSessionId)}
                      onChange={(event) =>
                        void w.changeModel(event.target.value)
                      }
                    >
                      {!w.config?.models.some(
                        (item) => item.id === w.model,
                      ) && (
                        <option value={w.model}>{w.model || '默认模型'}</option>
                      )}
                      {w.config?.models.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                    <ChevronDown size={12} />
                  </div>
                  <button
                    className="w2-send"
                    aria-label={w.busy ? '停止执行' : '发送消息'}
                    title={w.busy ? '停止执行' : '发送消息'}
                    disabled={
                      !w.busy &&
                      (!w.draft.trim() || !w.daemon || composerLocked)
                    }
                    onClick={() => void (w.busy ? w.stop() : w.send())}
                  >
                    {w.busy ? (
                      <Square size={13} fill="currentColor" />
                    ) : (
                      <ArrowUp size={18} />
                    )}
                  </button>
                </div>
              </div>
            </div>
          </section>

          <div
            className="w2-pane-resizer w2-right-resizer"
            role="separator"
            aria-label="调整右侧栏宽度"
            aria-orientation="vertical"
            aria-valuemin={MIN_RIGHT_WIDTH}
            aria-valuemax={MAX_RIGHT_WIDTH}
            aria-valuenow={rightWidth}
            tabIndex={0}
            onPointerDown={startResize('right')}
            onPointerMove={moveResize}
            onPointerUp={finishResize}
            onPointerCancel={finishResize}
            onDoubleClick={() => setPaneWidth('right', defaultRightWidth(leftWidth), true)}
            onKeyDown={event => {
              if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
              event.preventDefault()
              resizeWithKeyboard('right', event.key === 'ArrowRight' ? 1 : -1, event.shiftKey)
            }}
          />

          <section className="w2-right" aria-label="工作区面板">
            <header className="w2-panel-toolbar">
              <span>{panel !== 'home' && panelNames[panel]}</span>
              <div>
                {panel !== 'home' && (
                  <IconButton
                    label="返回工作区"
                    onClick={() => setPanel('home')}
                  >
                    <X />
                  </IconButton>
                )}
                <IconButton
                  label={fullscreen ? '退出全屏' : '全屏工作区'}
                  onClick={() => {
                    const action = document.fullscreenElement
                      ? document.exitFullscreen()
                      : document.documentElement.requestFullscreen()
                    action.catch(() => w.setError('浏览器未允许全屏'))
                  }}
                >
                  {fullscreen ? <Minimize2 /> : <Maximize2 />}
                </IconButton>
                {panelControls}
              </div>
            </header>
            <nav className="w2-right-nav" aria-label="会话详情导航">
              <button className={panel === 'context' ? 'selected' : ''} onClick={() => openPanel('context')}><File />上下文</button>
              <button className={panel === 'flow' ? 'selected' : ''} onClick={() => openPanel('flow')}><Workflow />依赖</button>
              <button className={panel === 'insights' ? 'selected' : ''} onClick={() => openPanel('insights')}><Lightbulb />洞察</button>
            </nav>
            {panel === 'home' ? (
              <div className="w2-launcher">
                <button aria-label="审查" onClick={() => openPanel('review')}>
                  <GitPullRequest />
                  <span>审查</span>
                  <kbd>Ctrl+Shift+G</kbd>
                </button>
                <button onClick={() => setTerminal(true)}>
                  <Terminal />
                  <span>终端</span>
                  <kbd>Ctrl+J</kbd>
                </button>
                <button onClick={() => openPanel('browser')}>
                  <Globe2 />
                  <span>浏览器</span>
                  <ArrowUpRight className="w2-launcher-arrow" />
                </button>
                <button onClick={() => openPanel('files')}>
                  <FolderClosed />
                  <span>文件</span>
                </button>
                <button onClick={() => openPanel('schedules')}><Clock3 /><span>定时任务</span></button>
                <button onClick={() => openPanel('plugins')}><Plug /><span>插件</span></button>
              </div>
            ) : (
              <div className="w2-panel-content">
                {panel === 'review' && <DiffPanel />}
                {panel === 'context' && <ContextCards />}
                {panel === 'flow' && <Flowchart key={w.sessionId || 'new'} />}
                {panel === 'insights' && <InsightCards onInspect={key => {
                  if (key === 'context') openPanel('context')
                  else setSummaryOpen(true)
                }} />}
                {panel === 'schedules' && <SchedulePanel />}
                {panel === 'plugins' && <PluginPanel />}
                {panel === 'files' && (
                  <>
                    <WorkspaceFileTree />
                    <div className="w2-file-divider" />
                    <div className="w2-section-heading">
                      <span>会话文件</span>
                      <button
                        disabled={uploadLocked}
                        onClick={() => fileInput.current?.click()}
                      >
                        <Plus size={14} />
                        添加文件
                      </button>
                    </div>
                    {!w.sources.length && (
                      <p className="w2-muted">还没有添加文件</p>
                    )}
                    {w.sources.map((source) => (
                      <label className="w2-file-row" key={source.id}>
                        <File size={16} />
                        <span>
                          {source.title}
                          <small>{source.path}</small>
                        </span>
                        <input
                          type="checkbox"
                          aria-label={`附加 ${source.title}`}
                          disabled={w.uploading}
                          checked={w.selectedSources.includes(source.id)}
                          onChange={(event) =>
                            w.setSelectedSources(
                              event.target.checked
                                ? [...w.selectedSources, source.id]
                                : w.selectedSources.filter(
                                    (id) => id !== source.id,
                                  ),
                            )
                          }
                        />
                      </label>
                    ))}
                  </>
                )}
                {panel === 'browser' && (
                  <form
                    className="w2-browser"
                    onSubmit={(event) => {
                      event.preventDefault()
                      try {
                        window.open(
                          safeWebUrl(browserUrl),
                          '_blank',
                          'noopener,noreferrer',
                        )
                      } catch (e) {
                        w.setError(errorText(e))
                      }
                    }}
                  >
                    <Globe2 size={28} />
                    <label htmlFor="w2-url">打开网页</label>
                    <div>
                      <input
                        id="w2-url"
                        placeholder="输入网址"
                        value={browserUrl}
                        onChange={(event) => setBrowserUrl(event.target.value)}
                        required
                      />
                      <button type="submit" aria-label="在新标签页打开">
                        <ArrowUpRight size={18} />
                      </button>
                    </div>
                    <p>在浏览器新标签页中打开</p>
                  </form>
                )}
                {panel === 'tools' && (
                  <>
                    <div className="w2-search w2-tool-search">
                      <Search size={14} />
                      <input
                        aria-label="搜索工具"
                        placeholder="搜索工具…"
                        value={toolQuery}
                        onChange={(event) => setToolQuery(event.target.value)}
                      />
                    </div>
                    {!w.config && (
                      <p className="w2-muted">
                        {w.daemon
                          ? '正在读取工具清单…'
                          : '连接服务后查看工具清单'}
                      </p>
                    )}
                    {w.config?.tools
                      .filter((tool) =>
                        `${tool.name} ${tool.description}`
                          .toLowerCase()
                          .includes(toolQuery.toLowerCase()),
                      )
                      .map((tool) => (
                        <details className="w2-tool" key={tool.name}>
                          <summary>
                            <Workflow size={15} />
                            {tool.name}
                          </summary>
                          <p>{tool.description}</p>
                        </details>
                      ))}
                  </>
                )}
                {panel === 'tasks' && (
                  <>
                    {!w.snapshot?.tasks.length && (
                      <p className="w2-muted">当前会话暂无任务</p>
                    )}
                    {w.snapshot?.tasks.map((task) => (
                      <div key={task.id} className="w2-task">
                        <span>
                          {task.status === 'completed' ? (
                            <Check size={16} />
                          ) : (
                            <Clock3 size={16} />
                          )}
                          {task.subject}
                        </span>
                        <small>
                          {
                            {
                              pending: '待开始',
                              in_progress: '进行中',
                              blocked: '等待处理',
                              completed: '已完成',
                            }[task.status]
                          }
                        </small>
                        <p>{task.description}</p>
                      </div>
                    ))}
                  </>
                )}
              </div>
            )}
          </section>
        </div>
        {terminal && (
          <section className="w2-terminal" aria-label="终端执行记录">
            <header>
              <span>
                <Terminal size={14} />
                执行记录
              </span>
              <span
                className="w2-terminal-path"
                title={w.daemon?.workspace_root}
              >
                {w.daemon?.workspace_root}
              </span>
              <IconButton label="收起执行面板" onClick={toggleTerminal}>
                <X />
              </IconButton>
            </header>
            <div className="w2-terminal-output">
              {w.liveEvents.length ? (
                w.liveEvents.map((event, index) => (
                  <div key={`${event.id}-${index}`}>
                    <span
                      className={
                        event.type === 'tool_call_error'
                          ? 'w2-log-error'
                          : 'w2-log-state'
                      }
                    >
                      {event.type === 'tool_call_start'
                        ? '执行'
                        : event.type === 'tool_call_error'
                          ? '失败'
                          : '完成'}
                    </span>{' '}
                    {String(event.data.name || event.data.tool_name || '工具')}
                    <pre>
                      {String(event.data.content || event.data.message || '')}
                    </pre>
                  </div>
                ))
              ) : w.runs.length ? (
                w.runs.slice(0, 8).map((run) => (
                  <details key={run.id}>
                    <summary>
                      {new Date(run.started_at).toLocaleString('zh-CN')} ·{' '}
                      {(
                        {
                          completed: '已完成',
                          success: '已完成',
                          failed: '失败',
                          running: '执行中',
                          cancelled: '已停止',
                        } as Record<string, string>
                      )[run.status] || run.status}
                    </summary>
                    {run.steps.map((step) => (
                      <div key={step.sequence}>
                        {step.summary}
                        {step.detail && <pre>{step.detail}</pre>}
                      </div>
                    ))}
                  </details>
                ))
              ) : (
                <p className="w2-terminal-empty">
                  {w.connecting ? '正在连接本地服务…' : '暂无执行记录'}
                </p>
              )}
            </div>
          </section>
        )}
      </main>
      <dialog
        ref={dialogRef}
        className={`w2-dialog ${dialog === 'settings' ? 'w2-settings-dialog' : ''}`}
        onCancel={() => setDialog(null)}
        onClick={(event) => {
          if (event.target === dialogRef.current) setDialog(null)
        }}
      >
        <div className="w2-dialog-inner">
          <header>
            <h2>
              {dialog === 'settings'
                ? '设置'
                : dialog === 'rename'
                  ? '修改会话名称'
                : dialog === 'delete'
                  ? '删除会话'
                  : '帮助与快捷键'}
            </h2>
            <IconButton label="关闭对话框" onClick={() => setDialog(null)}>
              <X />
            </IconButton>
          </header>
          {dialog === 'settings' && <SettingsPage />}
          {dialog === 'rename' && <form className="w2-rename-form" onSubmit={async event => {
            event.preventDefault()
            if (await w.renameSession(renameSessionId, renameTitle)) setDialog(null)
          }}><label>会话名称<input autoFocus maxLength={120} required value={renameTitle} onChange={event => setRenameTitle(event.target.value)} /></label><div className="w2-dialog-actions"><button type="button" onClick={() => setDialog(null)}>取消</button><button className="w2-primary" disabled={w.mutating || !renameTitle.trim()}>保存</button></div></form>}
          {dialog === 'help' && (
            <div className="w2-shortcuts">
              <p>
                <span>搜索会话</span>
                <kbd>Ctrl+K</kbd>
              </p>
              <p>
                <span>切换侧栏</span>
                <kbd>Ctrl+B</kbd>
              </p>
              <p>
                <span>执行记录</span>
                <kbd>Ctrl+J</kbd>
              </p>
              <p>
                <span>代码审查</span>
                <kbd>Ctrl+Shift+G</kbd>
              </p>
              <p>
                <span>发送消息 / 换行</span>
                <kbd>Enter / Shift+Enter</kbd>
              </p>
              <small>终端区域显示 Agent 的真实执行记录。</small>
            </div>
          )}
          {dialog === 'delete' && (
            <>
              <p>删除“{current?.title || '新对话'}”及其历史记录？</p>
              <div className="w2-dialog-actions">
                <button onClick={() => setDialog(null)}>取消</button>
                <button
                  className="w2-danger"
                  disabled={!current || currentWriteLocked(current.id)}
                  onClick={() => {
                    if (w.sessionId) void w.deleteSession(w.sessionId)
                    setDialog(null)
                  }}
                >
                  删除
                </button>
              </div>
            </>
          )}
        </div>
      </dialog>
    </div>
  )
}
