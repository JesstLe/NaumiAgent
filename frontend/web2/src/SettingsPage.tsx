import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Cable, MessageSquare, ShieldCheck, Info } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { usePlatform } from '@naumi/shared/platform'

const tabs = [
  { id: 'connection', title: '连接', icon: Cable },
  { id: 'chat', title: '对话', icon: MessageSquare },
  { id: 'permissions', title: '权限', icon: ShieldCheck },
  { id: 'about', title: '关于', icon: Info },
]

export function SettingsPage() {
  const w = useWorkspace()
  const platform = usePlatform()
  const [tab, setTab] = useState('connection')
  const [url, setUrl] = useState(w.base)
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState('')
  const [failed, setFailed] = useState(false)
  const locked = w.busy || w.uploading || w.mutating || saving
  useEffect(() => {
    let active = true
    platform.getToken().then(value => { if (active) setToken(value || '') }).catch(() => {
      if (active) { setFailed(true); setNotice('无法读取连接令牌，请重试') }
    })
    return () => { active = false }
  }, [platform])
  return <div className="w2-settings">
    <nav aria-label="设置分类">{tabs.map(item => <button key={item.id} aria-current={tab === item.id ? 'page' : undefined}
      onClick={() => { setTab(item.id); setNotice('') }}><item.icon size={16} />{item.title}</button>)}
      <Link to="/chat" className="w2-settings-legacy">打开原版 Web</Link>
    </nav>
    <section aria-label={tabs.find(item => item.id === tab)?.title}>
      <h3>{tabs.find(item => item.id === tab)?.title}</h3>
      {tab === 'connection' && <form onSubmit={async event => {
        event.preventDefault(); setSaving(true); setNotice(''); setFailed(false)
        try {
          await w.configure(url, token)
          setNotice('连接设置已保存')
        } catch (error) { setFailed(true); setNotice(error instanceof Error ? error.message : '连接未保存，请重试') }
        finally { setSaving(false) }
      }}>
        <label>API 地址<input required value={url} onChange={event => setUrl(event.target.value)} /></label>
        <label>连接令牌<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} placeholder="未启用认证时留空" /></label>
        <div className="w2-setting-row"><span>本地服务</span><span>{w.connecting ? '连接中' : w.daemon ? '已连接' : '未连接'}</span></div>
        <button className="w2-primary" disabled={locked}>{saving ? '正在连接…' : '保存并连接'}</button>
      </form>}
      {tab === 'chat' && <>
        <label className="w2-setting-row"><span>发送快捷键</span><select aria-label="发送快捷键" value={w.sendKey} onChange={event => w.setSendKey(event.target.value)}>
          <option value="enter">Enter 发送</option><option value="mod-enter">Ctrl / ⌘ + Enter 发送</option>
        </select></label>
        <label className="w2-setting-row"><span>{w.sessionId ? '当前会话模型' : '新对话模型'}</span><select aria-label="设置模型" value={w.model} disabled={locked || !w.config} onChange={event => void w.changeModel(event.target.value)}>
          {!w.config?.models.some(model => model.id === w.model) && <option value={w.model}>{w.model || '默认模型'}</option>}
          {w.config?.models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select></label>
        {w.config?.model_warnings.map(message => <p className="w2-muted" key={message}>{message}</p>)}
      </>}
      {tab === 'permissions' && <>
        <label className="w2-setting-row"><span>当前执行模式</span><select aria-label="设置执行模式" value={w.mode} disabled={locked} onChange={event => w.setMode(event.target.value as typeof w.mode)}>
          <option value="default">默认权限</option><option value="plan">计划模式</option><option value="bypass">跳过审批</option>
        </select></label>
        <p className="w2-setting-description">{w.mode === 'bypass' ? '跳过审批会允许 Agent 直接执行工具操作。' : w.mode === 'plan' ? '以计划模式发送下一条消息，具体工具权限由服务端控制。' : '工具操作按照本地服务的权限规则请求确认。'}</p>
        <div className="w2-setting-row"><span>等待审批</span><span>{w.permissions.length}</span></div>
      </>}
      {tab === 'about' && <dl className="w2-about">
        <dt>应用</dt><dd>NaumiAgent Web2</dd><dt>工作区</dt><dd>{w.daemon?.workspace_name || '未连接'}</dd>
        <dt>位置</dt><dd>{w.daemon?.workspace_root || '—'}</dd><dt>可用工具</dt><dd>{w.config?.tools.length ?? '—'}</dd>
      </dl>}
      {notice && <p role={failed ? 'alert' : 'status'} className={failed ? 'w2-error' : 'w2-muted'}>{notice}</p>}
      {w.error && !notice && <p role="alert" className="w2-error">{w.error}</p>}
    </section>
  </div>
}
