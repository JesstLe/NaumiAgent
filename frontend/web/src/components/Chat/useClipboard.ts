import { useState } from 'react'

export function useClipboard() {
  const [copied, setCopied] = useState(false)

  async function copyText(text: string): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
      return true
    } catch {
      setCopied(false)
      return false
    }
  }

  async function copyImage(url: string): Promise<boolean> {
    try {
      const response = await fetch(url)
      const blob = await response.blob()
      await navigator.clipboard.write([
        new ClipboardItem({ [blob.type]: blob }),
      ])
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
      return true
    } catch {
      // Fallback to copying the URL as text.
      return copyText(url)
    }
  }

  return { copied, copyText, copyImage }
}
