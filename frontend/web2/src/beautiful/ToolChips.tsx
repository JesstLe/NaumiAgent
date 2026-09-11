import type { ActivityStep, ActivityState } from '@naumi/shared/api/activity'
import Primitive from './upstream/components/primitives/ToolChips'
export const activityNames: Record<ActivityState, string> = { running: '执行中', completed: '已完成', failed: '失败', cancelled: '已停止', unknown: '状态待确认' }
export function ToolChips({ steps }: { steps: ActivityStep[] }) {
  return <Primitive steps={steps.map((row, index) => ({
    icon: /read|读取/i.test(row.label) ? 'read' : /write|edit|写|编辑/i.test(row.label) ? 'write' : 'run',
    label: `${index + 1}. ${row.label}`, chip: activityNames[row.state], mono: true, detailMono: true,
    detail: [...(row.input ? [{ text: `输入：${row.input}` }] : []), { text: row.output || (row.state === 'running' ? '等待工具结果…' : '此记录没有附带输出') }],
  }))} diffs={[]} diffLines={{}} labels={{ header: `${steps.length} 条工具记录`, more: '' }} />
}
