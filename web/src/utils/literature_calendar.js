// Calendar-only arithmetic uses UTC to avoid DST and browser time-zone shifts.
export function monthDays(month) {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month) || Number(month.slice(0, 4)) < 1000) return []
  const [year, number] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year, number - 1, 1))
  const count = new Date(Date.UTC(year, number, 0)).getUTCDate()
  const offset = (first.getUTCDay() + 6) % 7
  const cells = Array(offset).fill('')
  for (let day = 1; day <= count; day++) cells.push(`${month}-${String(day).padStart(2, '0')}`)
  while (cells.length % 7) cells.push('')
  return cells
}

export function adjacentMonth(month, offset) {
  if (!monthDays(month).length || !Number.isInteger(offset)) return ''
  const [year, number] = month.split('-').map(Number)
  const date = new Date(Date.UTC(year, number - 1 + offset, 1))
  if (date.getUTCFullYear() < 1000 || date.getUTCFullYear() > 9999) return ''
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`
}
