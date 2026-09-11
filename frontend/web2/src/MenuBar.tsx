import { useEffect, useRef, useState } from 'react'

export interface MenuAction {
  label: string
  run: () => void
  shortcut?: string
  disabled?: boolean
}

export function MenuBar({ menus }: { menus: { label: string; actions: MenuAction[] }[] }) {
  const [open, setOpen] = useState<number | null>(null)
  const root = useRef<HTMLElement>(null)
  const focusMenu = (index: number) => {
    const next = (index + menus.length) % menus.length
    setOpen(next)
    requestAnimationFrame(() => root.current?.querySelectorAll<HTMLElement>('[role="menu"] button:not(:disabled)')[0]?.focus())
  }
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(null)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [])
  return <nav ref={root} className="w2-menubar" aria-label="应用菜单" onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(null)
  }} onKeyDown={(event) => {
    if (open === null) return
    if (event.key === 'Escape') {
      event.preventDefault()
      root.current?.querySelectorAll<HTMLElement>('.w2-menu-trigger')[open]?.focus()
      setOpen(null)
    } else if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault()
      focusMenu(open + (event.key === 'ArrowRight' ? 1 : -1))
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      const items = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menu"] button:not(:disabled)') ?? [])
      const current = items.indexOf(document.activeElement as HTMLElement)
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
      items[index]?.focus()
    }
  }}>
    {menus.map((menu, index) => <div className="w2-menu" key={menu.label}>
      <button className="w2-menu-trigger" aria-haspopup="menu" aria-expanded={open === index}
        onClick={() => setOpen(open === index ? null : index)}
        onKeyDown={(event) => { if (event.key === 'ArrowDown') { event.preventDefault(); event.stopPropagation(); focusMenu(index) } }}>
        {menu.label}
      </button>
      {open === index && <div role="menu" aria-label={menu.label} className="w2-menu-popup">
        {menu.actions.map(action => <button role="menuitem" key={action.label} disabled={action.disabled}
          onClick={() => { setOpen(null); action.run() }}>
          <span>{action.label}</span><kbd>{action.shortcut}</kbd>
        </button>)}
      </div>}
    </div>)}
  </nav>
}
