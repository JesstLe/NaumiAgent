import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from '@tailwindcss/node'
import { Scanner } from '@tailwindcss/oxide'
import postcss from 'postcss'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/beautiful')
const compiler = await compile(await fs.readFile(path.join(root, 'upstream/foundation.css'), 'utf8'), { base: path.join(root, 'upstream'), onDependency() {} })
const scanner = new Scanner({ sources: compiler.sources })
const ast = postcss.parse(compiler.build(scanner.scan()))
// Keep the upstream cascade order, but isolate every rule from the host UI.
// Flattening layers prevents the host's unlayered CSS from overriding primitives.
ast.walkAtRules('layer', rule => { if (rule.nodes) rule.replaceWith(...rule.nodes); else rule.remove() })
ast.walkRules(rule => {
  let ancestor = rule.parent
  while (ancestor) { if (ancestor.type === 'rule' || (ancestor.type === 'atrule' && /keyframes$/.test(ancestor.name))) return; ancestor = ancestor.parent }
  if (rule.selector === 'body') { rule.remove(); return }
  rule.selectors = rule.selectors.map(selector => {
    if (/^(:root|:host|html)$/.test(selector)) return '.bui-root'
    if (selector === '*') return '.bui-root, .bui-root *'
    return `.bui-root ${selector}`
  })
})
await fs.writeFile(path.join(root, 'upstream.generated.css'), `/* Generated from the official Beautiful UI foundation. Run pnpm beautiful:css. */\n${ast.toString().trimEnd()}\n`)
