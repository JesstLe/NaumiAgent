import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ArrowUp,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  Copy,
  File,
  FolderClosed,
  FolderOpen,
  GitBranch,
  GitPullRequest,
  Globe2,
  HelpCircle,
  Loader2,
  ListTodo,
  Maximize2,
  Minimize2,
  PanelBottom,
  PanelLeft,
  PanelRight,
  Plus,
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
import './web2.css'
import { MenuBar } from './MenuBar'
import { SettingsPage } from './SettingsPage'
import { TodoPanel } from './TodoPanel'
import { GoalPanel } from './GoalPanel'
import { DiffPanel } from './DiffPanel'
import { ThinkingState } from './beautiful/ThinkingState'
import { SelectionActions } from './beautiful/SelectionActions'
import { ContextCards } from './beautiful/ContextCards'
import { Flowchart } from './beautiful/Flowchart'
import { InsightCards } from './beautiful/InsightCards'
import './beautiful/upstream.generated.css'
import './beautiful/beautiful.css'
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import { useCommandCompletion } from '@naumi/shared/hooks/useCommandCompletion'

type Panel = 'home' | 'review' | 'files' | 'browser' | 'tools' | 'tasks'
const panelNames: Record<Panel, string> = {
  home: '工作区',
  review: '审查',
  files: '文件',
  browser: '浏览器',
  tools: '工具与扩展',
  tasks: '任务',
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
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const w = useWorkspace()
  return (
    <IconButton
      label={copied ? '已复制' : '复制消息'}
      onClick={() => {
        navigator.clipboard
          .writeText(text)
          .then(() => setCopied(true))
          .catch(() => w.setError('复制失败，请选中文字复制'))
      }}
    >
      {copied ? <Check /> : <Copy />}
    </IconButton>
  )
}
function MessageContent({ content }: { content: string }) {
  return (
    <>
      {content.split(/(```[\s\S]*?```)/g).map((part, index) => {
        if (part.startsWith('```')) {
          const newline = part.indexOf('\n')
          return (
            <pre key={index}>
              <code>{part.slice(newline === -1 ? 3 : newline + 1, -3)}</code>
            </pre>
          )
        }
        return (
          <div className="w2-prose" key={index}>
            {part}
          </div>
        )
      })}
    </>
  )
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
  const [panel, setPanel] = useState<Panel>('home')
  const [summary, setSummary] = useState<'todos' | 'goal' | 'context' | 'flow' | 'insights' | null>(null)
  const [expanded, setExpanded] = useState(true)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')
  const [toolQuery, setToolQuery] = useState('')
  const [browserUrl, setBrowserUrl] = useState('')
  const [dialog, setDialog] = useState<'settings' | 'help' | 'delete' | null>(
    null,
  )
  const [fullscreen, setFullscreen] = useState(false)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const followOutput = useRef(true)
  const workspace = w.daemon?.workspace_name || 'NaumiAgent'
  const current = w.sessions.find((item) => item.id === w.sessionId)
  const messages = w.messages.filter(
    (item) => ['user', 'assistant'].includes(item.role) && item.content,
  )
  const locked = w.busy || w.uploading || w.mutating
  const branch = w.diff?.branch || w.snapshot?.worktrees[0]?.branch
  const filteredSessions = w.sessions.filter((item) =>
    (item.title || item.id).toLowerCase().includes(query.trim().toLowerCase()),
  )

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
      <IconButton label="切换执行面板" active={terminal} onClick={toggleTerminal}><PanelBottom /></IconButton>
      <IconButton label="切换右侧面板" active={right} onClick={toggleRight}><PanelRight /></IconButton>
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
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSearching(false)
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
      className={`web2 ${sidebar ? '' : 'w2-sidebar-hidden'} ${right ? '' : 'w2-right-hidden'}`}
    >
      <SelectionActions focusComposer={() => textarea.current?.focus()} />
      <header className="w2-titlebar">
        <MenuBar menus={[
          { label: '文件', actions: [
            { label: '新建对话', run: newChat, disabled: locked },
            { label: '添加文件', run: () => fileInput.current?.click(), disabled: locked || !w.daemon },
            { label: '导出对话', disabled: !messages.length, run: () => {
              const blob = new Blob([messages.map(item => `## ${item.role === 'user' ? '用户' : 'NaumiAgent'}\n\n${item.content}`).join('\n\n')], { type: 'text/markdown;charset=utf-8' })
              const url = URL.createObjectURL(blob)
              const link = document.createElement('a')
              link.href = url; link.download = `naumi-${w.sessionId || 'chat'}.md`; link.click()
              setTimeout(() => URL.revokeObjectURL(url), 1000)
            } },
            { label: '设置', run: () => setDialog('settings') },
            { label: '删除当前会话', disabled: !current || locked, run: () => setDialog('delete') },
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
            { label: '待办', run: () => setSummary('todos') },
            { label: '目标', run: () => setSummary('goal') },
            { label: '上下文快照', run: () => setSummary('context') },
            { label: '任务依赖图', run: () => setSummary('flow') },
            { label: '会话洞察', run: () => setSummary('insights') },
            { label: '任务记录', run: () => openPanel('tasks') },
            { label: fullscreen ? '退出全屏' : '进入全屏', run: () => { void (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => w.setError('浏览器暂不支持全屏')) } },
          ] },
          { label: '帮助', actions: [
            { label: '快捷键', run: () => setDialog('help') },
            { label: '工具与扩展', run: () => openPanel('tools') },
            { label: '重新连接', disabled: locked || w.connecting, run: () => { void w.connect() } },
          ] },
        ]} />
        <IconButton label="切换侧栏" onClick={toggleSidebar}>
          <PanelLeft />
        </IconButton>
        <span>NaumiAgent</span>
        <span className="w2-title-divider">/</span>
        <span>{workspace}</span>
        <div className="w2-title-end">
          <span className={`w2-connection-dot ${w.daemon ? 'online' : ''}`} />
          {w.connecting ? '连接中' : w.daemon ? '本地工作区' : '未连接'}
        </div>
      </header>
      <aside className="w2-sidebar" aria-label="项目侧栏">
        <div className="w2-brand">
          <button onClick={newChat} disabled={locked}>
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
          <button disabled={locked} onClick={newChat}>
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
          <IconButton label="新建对话" disabled={locked} onClick={newChat}>
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
                <button
                  key={session.id}
                  className={session.id === w.sessionId ? 'selected' : ''}
                  aria-current={session.id === w.sessionId ? 'page' : undefined}
                  title={session.title || '新对话'}
                  disabled={locked}
                  onClick={() => void w.select(session.id)}
                >
                  <span>{session.title || '新对话'}</span>
                  {w.busy && session.id === w.sessionId && (
                    <Loader2 className="w2-spin" size={13} />
                  )}
                </button>
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

      <main className={`w2-main ${terminal ? 'has-terminal' : ''}`}>
        <div className="w2-workspace">
          <section className="w2-chat" aria-label="对话">
            <div className="w2-chat-heading">
              <span>{current?.title || '新对话'}</span>
              <IconButton label="切换对话摘要" active={summary !== null} onClick={() => setSummary(value => value ? null : 'todos')}><ListTodo /></IconButton>
              <div className="w2-mobile-tools"><IconButton label="打开文件面板" onClick={() => openPanel('files')}><FolderClosed /></IconButton></div>
              <div className={`w2-chat-panel-controls ${right ? 'right-open' : ''}`}>{panelControls}</div>
            </div>
            {summary && <section className="w2-chat-summary" aria-label="对话摘要">
              <div className="w2-summary-heading">
                <button aria-pressed={summary === 'todos'} onClick={() => setSummary('todos')}>待办</button>
                <button aria-pressed={summary === 'goal'} onClick={() => setSummary('goal')}>目标</button>
                <button aria-pressed={summary === 'context'} onClick={() => setSummary('context')}>上下文</button>
                <button aria-pressed={summary === 'flow'} onClick={() => setSummary('flow')}>依赖</button>
                <button aria-pressed={summary === 'insights'} onClick={() => setSummary('insights')}>洞察</button>
                <IconButton label="收起对话摘要" onClick={() => setSummary(null)}><X /></IconButton>
              </div>
              {summary === 'todos' && <TodoPanel key={w.sessionId || 'new'} />}
              {summary === 'goal' && <GoalPanel />}
              {summary === 'context' && <ContextCards />}
              {summary === 'flow' && <Flowchart key={w.sessionId || 'new'} />}
              {summary === 'insights' && <InsightCards onInspect={key => setSummary(key === 'context' ? 'context' : 'todos')} />}
            </section>}
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
              {!messages.length && !w.loading && !w.runs.length && !w.liveEvents.length ? (
                <div className="w2-welcome">
                  <Logo className="w2-welcome-logo" />
                  <h1>
                    你想让我们在 <span>{workspace}</span> 中构建什么？
                  </h1>
                </div>
              ) : (
                <div className="w2-message-list">
                  {w.loading && <p className="w2-muted">正在加载会话…</p>}
                  {messages.map((message) => (
                    <article
                      key={message.id}
                      className={`w2-message ${message.role}`}
                    >
                      <MessageContent content={message.content} />
                      <div className="w2-message-actions">
                        <CopyButton text={message.content} />
                      </div>
                    </article>
                  ))}
                  <ThinkingState />
                  {w.busy && (
                    <div role="status" className="w2-working">
                      <Loader2 className="w2-spin" size={15} />
                      {w.permissions.length ? '等待你的确认' : '正在执行…'}
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="w2-composer-wrap">
              {!summary && <TodoPanel compact onExpand={() => setSummary('todos')} />}
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
                  if (!locked) void w.upload(event.dataTransfer.files)
                }}
              >
                <textarea
                  ref={textarea}
                  aria-label="消息"
                  aria-controls={completion.items.length ? 'w2-command-list' : undefined}
                  aria-activedescendant={completion.items.length ? `w2-command-${completion.index}` : undefined}
                  placeholder="描述任务，或输入 / 使用命令"
                  value={w.draft}
                  disabled={w.busy || w.loading || w.connecting}
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
                            disabled={w.busy}
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
                    disabled={locked || !w.daemon}
                    onClick={() => fileInput.current?.click()}
                  >
                    {w.uploading ? <Loader2 className="w2-spin" /> : <Plus />}
                  </IconButton>
                  <label className="w2-mode" title="执行权限">
                    <ShieldCheck size={14} />
                    <select
                      aria-label="执行模式"
                      value={w.mode}
                      disabled={locked}
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
                      disabled={locked || !w.daemon}
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
                      (!w.draft.trim() || !w.daemon || locked || w.loading)
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
              </div>
            ) : (
              <div className="w2-panel-content">
                {panel === 'review' && <DiffPanel />}
                {panel === 'files' && (
                  <>
                    <div className="w2-section-heading">
                      <span>会话文件</span>
                      <button
                        disabled={locked || !w.daemon}
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
                          disabled={locked}
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
                : dialog === 'delete'
                  ? '删除会话'
                  : '帮助与快捷键'}
            </h2>
            <IconButton label="关闭对话框" onClick={() => setDialog(null)}>
              <X />
            </IconButton>
          </header>
          {dialog === 'settings' && <SettingsPage />}
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
                  disabled={locked}
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
