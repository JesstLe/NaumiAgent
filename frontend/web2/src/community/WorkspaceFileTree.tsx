import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import {
  hotkeysCoreFeature,
  selectionFeature,
  syncDataLoaderFeature,
} from '@headless-tree/core'
import { AssistiveTreeDescription, useTree } from '@headless-tree/react'
import {
  Braces,
  ChevronDown,
  Code2,
  File,
  FileImage,
  FileJson,
  FileText,
  Folder,
  RefreshCw,
} from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { errorText } from '@naumi/shared/hooks/useWorkspaceController'
import type {
  WorkspaceTreeItem,
  WorkspaceTreeResponse,
} from '@naumi/shared/api/types'
import './community.css'

const INDENT = 18

function FileIcon({ extension }: { extension: string }) {
  if (['tsx', 'jsx'].includes(extension)) return <Braces aria-hidden />
  if (['ts', 'js', 'mjs', 'cjs', 'py', 'rs', 'go', 'java', 'c', 'cpp', 'h'].includes(extension)) return <Code2 aria-hidden />
  if (['json', 'jsonl', 'yaml', 'yml', 'toml'].includes(extension)) return <FileJson aria-hidden />
  if (['svg', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'ico'].includes(extension)) return <FileImage aria-hidden />
  if (['md', 'mdx', 'txt', 'rst'].includes(extension)) return <FileText aria-hidden />
  return <File aria-hidden />
}

function LoadedTree({ data }: { data: WorkspaceTreeResponse }) {
  const expandedItems = useMemo(
    () => [data.root_id, ...data.items[data.root_id].children.filter(id => data.items[id]?.kind === 'directory').slice(0, 1)],
    [data],
  )
  const tree = useTree<WorkspaceTreeItem>({
    rootItemId: data.root_id,
    dataLoader: {
      getItem: id => data.items[id],
      getChildren: id => data.items[id]?.children ?? [],
    },
    features: [syncDataLoaderFeature, selectionFeature, hotkeysCoreFeature],
    getItemName: item => item.getItemData()?.name ?? item.getId(),
    isItemFolder: item => item.getItemData()?.kind === 'directory',
    initialState: { expandedItems },
    indent: INDENT,
  })

  return <div
    className="community-file-tree"
    style={{ '--community-tree-indent': `${INDENT}px` } as CSSProperties}
    {...tree.getContainerProps()}
  >
    <AssistiveTreeDescription tree={tree} />
    {tree.getItems().map(item => {
      const entry = item.getItemData()
      const level = item.getItemMeta().level
      return <button
        className="community-tree-item"
        key={item.getId()}
        title={entry.path}
        style={{ '--community-tree-level': level } as CSSProperties}
        data-folder={item.isFolder() || undefined}
        data-selected={item.isSelected() || undefined}
        data-focus={item.isFocused() || undefined}
        aria-expanded={item.isFolder() ? item.isExpanded() : undefined}
        {...item.getProps()}
      >
        <span className="community-tree-label">
          {item.isFolder()
            ? <><ChevronDown className="community-tree-chevron" aria-hidden /><Folder className="community-tree-file-icon" aria-hidden /></>
            : <FileIcon extension={entry.extension} />}
          <span>{item.getItemName()}</span>
          {entry.unreadable && <small>无法读取</small>}
        </span>
      </button>
    })}
  </div>
}

export function WorkspaceFileTree() {
  const w = useWorkspace()
  const [data, setData] = useState<WorkspaceTreeResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState('')

  const load = async () => {
    setLoading(true)
    setFailure('')
    try {
      setData(await w.api.workspaceTree())
    } catch (error) {
      setFailure(errorText(error))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [w.api, w.daemon?.workspace_root])

  return <section className="community-tree-section" aria-label="工作区文件">
    <div className="w2-section-heading">
      <span>工作区文件</span>
      <button type="button" disabled={loading || !w.daemon} onClick={() => void load()}>
        <RefreshCw size={13} className={loading ? 'w2-spin' : ''} />
        刷新
      </button>
    </div>
    {failure && <p className="community-tree-status" role="alert">目录读取失败：{failure}</p>}
    {!failure && loading && !data && <p className="community-tree-status">正在读取工作区…</p>}
    {data && <>
      <LoadedTree key={`${data.workspace_root}:${Object.keys(data.items).length}`} data={data} />
      {data.truncated && <p className="community-tree-limit" role="status">目录较大，当前显示前 {data.max_items} 项。</p>}
    </>}
  </section>
}
