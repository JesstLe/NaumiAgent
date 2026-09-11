/** Count observed states; percentages use largest remainders and total exactly 100. */
export function distribution(values: string[], labels: Record<string, string>) {
  const counts = new Map<string, number>()
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1)
  const rows = [...counts].map(([state, count]) => ({ state, label: labels[state] || state, count, units: Math.floor(count / values.length * 1000), remainder: count / values.length * 1000 % 1 }))
  const extra = 1000 - rows.reduce((sum, row) => sum + row.units, 0)
  const order = [...rows].sort((a, b) => b.remainder - a.remainder)
  for (let i = 0; i < Math.min(extra, order.length); i++) order[i].units++
  return rows.map(({ state, label, count, units }) => ({ state, label, count, percent: units / 10 }))
}
