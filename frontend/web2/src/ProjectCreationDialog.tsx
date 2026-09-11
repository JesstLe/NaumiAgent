import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, FolderClosed, Globe2, Laptop, Loader2, Plus, X } from 'lucide-react'

export interface ProjectCreationInput {
  name: string
  path: string
  location: 'local'
}

function folderName(path: string) {
  return path.replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean).at(-1) || ''
}

export function ProjectCreationDialog({
  open,
  canSelectLocal,
  busy,
  initialPath,
  onClose,
  onSelectDirectory,
  onCreate,
}: {
  open: boolean
  canSelectLocal: boolean
  busy: boolean
  initialPath?: string
  onClose: () => void
  onSelectDirectory: (initialPath?: string) => Promise<string | null>
  onCreate: (input: ProjectCreationInput) => Promise<void>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [step, setStep] = useState<'type' | 'details'>('type')
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [selecting, setSelecting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const element = dialog.current
    if (!element) return
    if (open) {
      setStep('type')
      setName('')
      setPath('')
      setError('')
      if (!element.open) {
        if (element.showModal) element.showModal()
        else element.setAttribute('open', '')
      }
    } else if (element.open) element.close()
  }, [open])

  const chooseDirectory = async () => {
    if (!canSelectLocal || selecting || busy) return
    setSelecting(true)
    setError('')
    try {
      const selected = await onSelectDirectory(path || initialPath)
      if (!selected) return
      setPath(selected)
      if (!name.trim()) setName(folderName(selected))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '未能选择项目文件夹')
    } finally {
      setSelecting(false)
    }
  }

  const create = async () => {
    if (!name.trim() || !path || busy) return
    setError('')
    try {
      await onCreate({ name: name.trim(), path, location: 'local' })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '项目创建失败')
    }
  }

  return (
    <dialog
      ref={dialog}
      className="w2-project-dialog"
      aria-labelledby="w2-project-dialog-title"
      onCancel={(event) => {
        event.preventDefault()
        if (!busy && !selecting) onClose()
      }}
    >
      <div className="w2-project-dialog-inner">
        <header>
          <div>
            {step === 'details' && (
              <button type="button" className="w2-project-back" aria-label="返回项目类型" onClick={() => setStep('type')}>
                <ArrowLeft />
              </button>
            )}
            <h2 id="w2-project-dialog-title">创建项目</h2>
          </div>
          <button type="button" className="w2-project-close" aria-label="关闭创建项目" disabled={busy || selecting} onClick={onClose}>
            <X />
          </button>
        </header>

        {step === 'type' ? (
          <>
            <p className="w2-project-field-title">项目类型</p>
            <div className="w2-project-types" role="radiogroup" aria-label="项目类型">
              <button type="button" role="radio" aria-checked="true" className="selected">
                <span><Laptop /><i /></span>
                <strong>本地</strong>
                <small>在你的电脑上编辑、运行和测试文件</small>
              </button>
              <button type="button" role="radio" aria-checked="false" disabled title="尚未连接远程主机">
                <span><Globe2 /><i /></span>
                <strong>远程</strong>
                <small>连接远程主机后选择它的文件夹</small>
              </button>
            </div>
            {!canSelectLocal && (
              <p className="w2-project-notice">本地项目创建需要 NaumiAgent 桌面版。</p>
            )}
            <footer>
              <button type="button" className="w2-project-next" disabled={!canSelectLocal || busy} onClick={() => setStep('details')}>
                下一步
              </button>
            </footer>
          </>
        ) : (
          <>
            <label className="w2-project-name">
              <span>项目名称</span>
              <div><FolderClosed /><input autoFocus maxLength={80} placeholder="项目名称" value={name} onChange={(event) => setName(event.target.value)} /></div>
            </label>
            <div className="w2-project-source">
              <p className="w2-project-field-title">源文件夹</p>
              <button type="button" disabled={!canSelectLocal || selecting || busy} onClick={() => void chooseDirectory()}>
                {selecting ? <Loader2 className="w2-spin" /> : <Plus />}
                <strong>{path ? folderName(path) : '添加 NaumiAgent 可读取和编辑的文件夹'}</strong>
                {path && <small title={path}>{path}</small>}
              </button>
            </div>
            {error && <p className="w2-project-error" role="alert">{error}</p>}
            <footer>
              <button type="button" disabled={busy || selecting} onClick={onClose}>取消</button>
              <button type="button" className="w2-project-create" disabled={!name.trim() || !path || busy || selecting} onClick={() => void create()}>
                {busy && <Loader2 className="w2-spin" />}
                {busy ? '正在创建…' : '创建项目'}
              </button>
            </footer>
          </>
        )}
      </div>
    </dialog>
  )
}
