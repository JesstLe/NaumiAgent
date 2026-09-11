import { useEffect, useState, type KeyboardEvent } from 'react'
import type { SlashCommand } from '../api/WorkbenchRuntimeClient'

export function useCommandCompletion(commands: SlashCommand[], draft: string, setDraft: (value: string) => void) {
  const [index, setIndex] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  useEffect(() => { setIndex(0); setDismissed(false) }, [draft])
  const term = draft.slice(1).toLowerCase()
  const items = /^\/[^\s]*$/.test(draft) && !dismissed ? commands.filter(item =>
    item.command.toLowerCase().includes(term) || item.aliases.some(alias => alias.includes(term)) || item.description.includes(term)
  ).slice(0, 12) : []
  const pick = (command: SlashCommand) => { setDraft(`${command.command} `); setDismissed(true) }
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!items.length || event.nativeEvent.isComposing || event.keyCode === 229) return false
    if (event.key === 'Escape') { setDismissed(true); event.preventDefault(); event.stopPropagation(); return true }
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); setIndex(previous => (previous + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length); return true
    }
    if (['Enter', 'Tab'].includes(event.key) && !event.shiftKey) { event.preventDefault(); pick(items[index] ?? items[0]); return true }
    return false
  }
  return { items, index, pick, onKeyDown }
}
