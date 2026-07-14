import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Copy, Check, Pencil } from 'lucide-react'
import { useClipboard } from './useClipboard'
import type { MessageResponse } from '@/api/types'

interface MessageBubbleProps {
  message: MessageResponse
  onEdit?: (message: MessageResponse, newContent: string) => void
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'])
const DATA_IMAGE_RE = /^data:image\/(png|jpeg|jpg|gif|webp|svg);base64,/

function looksLikeImageUrl(content: string): boolean {
  const trimmed = content.trim()
  // Direct URL to an image file.
  if (/^https?:\/\//i.test(trimmed)) {
    const ext = trimmed.split('?')[0].split('.').pop()?.toLowerCase()
    if (ext && IMAGE_EXTENSIONS.has(ext)) return true
  }
  // Markdown image syntax.
  if (/^!\[.*?\]\(https?:\/\/[^\s)]+\)$/i.test(trimmed)) return true
  // Data URL image.
  if (DATA_IMAGE_RE.test(trimmed)) return true
  return false
}

function extractImageUrl(content: string): string {
  const trimmed = content.trim()
  const markdownMatch = trimmed.match(/^!\[.*?\]\((https?:\/\/[^\s)]+)\)$/i)
  if (markdownMatch) return markdownMatch[1]
  return trimmed
}

export function MessageBubble({ message, onEdit }: MessageBubbleProps) {
  const { t } = useTranslation()
  const { copied, copyText, copyImage } = useClipboard()
  const [isEditing, setIsEditing] = useState(false)
  const [editContent, setEditContent] = useState(message.content)
  const isUser = message.role === 'user'
  const isImage = looksLikeImageUrl(message.content)
  const imageUrl = isImage ? extractImageUrl(message.content) : null

  const handleCopy = () => {
    if (isImage && imageUrl) {
      void copyImage(imageUrl)
    } else {
      void copyText(message.content)
    }
  }

  const handleSaveEdit = () => {
    if (!editContent.trim()) return
    onEdit?.(message, editContent.trim())
    setIsEditing(false)
  }

  return (
    <div
      className={`group relative max-w-3xl rounded-lg px-4 py-3 text-sm ${
        isUser
          ? 'ml-auto bg-blue-600 text-white'
          : 'bg-white text-neutral-800 shadow-sm'
      }`}
    >
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          {isEditing ? (
            <div className="flex flex-col gap-2">
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1 text-neutral-800 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                rows={3}
              />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditContent(message.content)
                    setIsEditing(false)
                  }}
                  className="px-2 py-1 text-xs rounded hover:bg-white/20"
                >
                  {t('taskMarket.cancel')}
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  className="px-2 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-700"
                >
                  {t('action.send')}
                </button>
              </div>
            </div>
          ) : (
            <>
              {isImage && imageUrl ? (
                <div className="space-y-2">
                  <img
                    src={imageUrl}
                    alt="generated"
                    className="max-w-full max-h-80 rounded-md border border-neutral-200"
                    loading="lazy"
                  />
                  <div className="text-xs text-neutral-500 truncate">{imageUrl}</div>
                </div>
              ) : (
                <div className="whitespace-pre-wrap">{message.content}</div>
              )}
              <div className={`mt-1 flex items-center gap-2 text-[10px] ${isUser ? 'text-blue-100' : 'text-neutral-400'}`}>
                <span>{new Date(message.timestamp).toLocaleString()}</span>
                {message.model && <span>· {message.model}</span>}
              </div>
            </>
          )}
        </div>

        {!isEditing && (
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
          >
            {isUser && onEdit && (
              <button
                type="button"
                onClick={() => setIsEditing(true)}
                title={t('chat.editMessage')}
                className={`rounded p-1 ${isUser ? 'hover:bg-blue-500 text-blue-50' : 'hover:bg-neutral-100 text-neutral-500'}`}
              >
                <Pencil className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              type="button"
              onClick={handleCopy}
              title={isImage ? t('chat.copyImage') : t('chat.copyOutput')}
              className={`rounded p-1 ${isUser ? 'hover:bg-blue-500 text-blue-50' : 'hover:bg-neutral-100 text-neutral-500'}`}
            >
              {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
