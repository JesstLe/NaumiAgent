import type { Todo } from './WorkbenchRuntimeClient'
export function dependencyGraph(todos: Todo[]) {
  const ids = new Set(todos.map(todo => todo.id))
  const edges = todos.flatMap(todo => [...new Set(todo.blocked_by)].filter(id => ids.has(id)).map(id => ({ from: id, to: todo.id })))
  const missing = todos.flatMap(todo => todo.blocked_by.filter(id => !ids.has(id)).map(id => `#${todo.id} 的依赖 #${id} 不在当前清单中`))
  const remaining = new Set(ids)
  const ordered: Todo[] = []
  while (remaining.size) {
    const available = todos.filter(todo => remaining.has(todo.id) && !edges.some(edge => edge.to === todo.id && remaining.has(edge.from)))
    if (!available.length) break
    for (const todo of available) { remaining.delete(todo.id); ordered.push(todo) }
  }
  // Retain nodes involved in cycles and warn; do not silently discard their edges.
  const cyclic = [...remaining]
  ordered.push(...todos.filter(todo => remaining.has(todo.id)))
  return { ordered, edges, warnings: [...missing, ...(cyclic.length ? [`依赖存在循环或被循环阻塞：${cyclic.map(id => `#${id}`).join('、')}`] : [])] }
}
