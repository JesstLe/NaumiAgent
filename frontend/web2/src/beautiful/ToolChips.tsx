// Adapted from Beautiful UI ToolChips, MIT (see LICENSE).
// Runtime-driven rows replace demo sequencing and duplicate diff previews.
import { Check, ChevronDown, CircleHelp, Loader2, Terminal, X } from 'lucide-react'
import type { ActivityStep, ActivityState } from '@naumi/shared/api/activity'

export const activityNames: Record<ActivityState, string> = { running: '执行中', completed: '已完成', failed: '失败', cancelled: '已停止', unknown: '状态待确认' }
export function ToolChips({ steps }: { steps: ActivityStep[] }) {
  return <div className="bui-tool-chips">{steps.map(row => <details className="bui-tool-row" key={row.id}>
    <summary>
      <span className="bui-tool-glyph"><Terminal size={13} /><ChevronDown size={13} /></span>
      <span className="bui-tool-label">{row.label}</span>
      <span className={`bui-chip ${row.state}`}>
        {row.state === 'running' ? <Loader2 className="w2-spin" size={12} /> : row.state === 'completed' ? <Check size={12} /> : row.state === 'failed' ? <X size={12} /> : <CircleHelp size={12} />}
        {activityNames[row.state]}
      </span>
    </summary>
    <div className="bui-tool-detail">
      {row.input && <><span>输入</span><pre>{row.input}</pre></>}
      {row.output ? <><span>输出</span><pre>{row.output}</pre></> : <p>{row.state === 'running' ? '等待工具结果…' : '此记录没有附带输出'}</p>}
    </div>
  </details>)}</div>
}
