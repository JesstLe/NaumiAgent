import type { ExecutionTimelineStep } from '@naumi/shared/api/activity'
import Primitive from './upstream/components/primitives/ToolChips'
import { toolChipData } from './toolChipData'
export { activityNames } from './toolChipData'

export function ToolChips({ steps, status, working = false }: { steps: ExecutionTimelineStep[]; status: string; working?: boolean }) {
  const data = toolChipData(steps)
  const tools = steps.filter(step => step.kind !== 'reasoning').length
  const messages = steps.length - tools
  return <Primitive {...data} headerLabel={working ? '正在推理' : undefined}
    labels={{ header: `${tools} 次工具调用，${messages} 条摘要 · ${working ? '执行中' : status}`, more: '' }} />
}
