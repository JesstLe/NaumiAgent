import type { ExecutionTimelineStep } from '@naumi/shared/api/activity'
import Primitive, { type ToolOutputBlock, type ToolStep } from './upstream/components/primitives/ToolChips'
import { toolChipData } from './toolChipData'
import { CodeBlock } from '../rich/MessageContent'
import { UnifiedDiff } from '../UnifiedDiff'
export { activityNames } from './toolChipData'

const renderOutput = (output: ToolOutputBlock, row: ToolStep) => <div className="bui-tool-output">
  {output.kind === 'diff'
    ? <UnifiedDiff patch={output.content} label={`${output.label || row.chip} 差异`} className="bui-tool-diff" />
    : <CodeBlock code={output.content} language={output.language} />}
</div>

export function ToolChips({
  steps,
  status,
  working = false,
  showHeader = true,
}: {
  steps: ExecutionTimelineStep[]
  status: string
  working?: boolean
  showHeader?: boolean
}) {
  const data = toolChipData(steps)
  const tools = steps.filter(step => step.kind !== 'reasoning').length
  const messages = steps.length - tools
  return <Primitive {...data} showHeader={showHeader} headerLabel={working ? '正在推理' : undefined}
    renderOutput={renderOutput}
    labels={{ header: `${tools} 次工具调用，${messages} 条摘要 · ${working ? '执行中' : status}`, more: '' }} />
}
