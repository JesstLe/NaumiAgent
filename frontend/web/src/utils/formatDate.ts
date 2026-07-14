// Shared date formatting utilities.
export function formatDate(date: string | Date): string {
  return new Date(date).toLocaleString('zh-CN')
}
