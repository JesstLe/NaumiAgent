import { useEffect, useRef } from 'react'
import './community.css'

function seedValue(seed: string) {
  let hash = 2166136261
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

function random(value: number) {
  let state = value || 1
  return () => {
    state ^= state << 13
    state ^= state >>> 17
    state ^= state << 5
    return (state >>> 0) / 4294967296
  }
}

export function AgentAvatar({ seed, size = 28, working = false }: { seed: string; size?: number; working?: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const element = canvas.current
    if (!element) return
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    element.width = Math.round(size * ratio)
    element.height = Math.round(size * ratio)
    element.style.width = `${size}px`
    element.style.height = `${size}px`
    const context = element.getContext('2d')
    if (!context) return
    context.scale(ratio, ratio)
    context.imageSmoothingEnabled = false
    context.clearRect(0, 0, size, size)

    const rng = random(seedValue(seed))
    const palette = [
      ['#eff3e9', '#52634a', '#9eb08f'],
      ['#f4eee5', '#6a5849', '#c1a98e'],
      ['#eaf0f2', '#475f68', '#91aeb6'],
      ['#f2ebef', '#66505f', '#b99aaa'],
    ][Math.floor(rng() * 4)]
    context.fillStyle = palette[0]
    context.fillRect(0, 0, size, size)
    const cells = 7
    const padding = Math.max(2, Math.floor(size * .12))
    const unit = (size - padding * 2) / cells
    for (let y = 0; y < cells; y += 1) {
      for (let x = 0; x < Math.ceil(cells / 2); x += 1) {
        if (rng() < .47) continue
        context.fillStyle = rng() > .72 ? palette[2] : palette[1]
        const left = Math.round(padding + x * unit)
        const top = Math.round(padding + y * unit)
        const width = Math.ceil(unit)
        const right = Math.round(padding + (cells - x - 1) * unit)
        context.fillRect(left, top, width, width)
        context.fillRect(right, top, width, width)
      }
    }
  }, [seed, size])

  return <span className="community-avatar" data-working={working || undefined} style={{ width: size, height: size }}>
    <canvas ref={canvas} role="img" aria-label="NaumiAgent 头像" />
  </span>
}
