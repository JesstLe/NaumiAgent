/** Rasterize the canonical SVG locally without a web server or network requests. */
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { chromium } = require('@playwright/test')
const svg = await readFile(path.join(root, 'assets/logo.svg'), 'utf8')
const output = path.resolve(process.argv[2] || path.join(root, 'assets/logo.png'))
const browser = await chromium.launch({
  headless: true,
  ...(process.env.BRAND_CHROMIUM_PATH ? { executablePath: process.env.BRAND_CHROMIUM_PATH } : {}),
})
try {
  const page = await browser.newPage()
  const base64 = await page.evaluate(async (source) => {
    const url = URL.createObjectURL(new Blob([source], { type: 'image/svg+xml' }))
    try {
      const image = new Image()
      image.src = url
      await image.decode()
      const canvas = document.createElement('canvas')
      canvas.width = canvas.height = 1024
      canvas.getContext('2d').drawImage(image, 0, 0, 1024, 1024)
      return canvas.toDataURL('image/png').split(',')[1]
    } finally {
      URL.revokeObjectURL(url)
    }
  }, svg)
  await writeFile(output, Buffer.from(base64, 'base64'))
} finally {
  await browser.close()
}
