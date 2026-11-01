export function activityWeeks(days) {
  if (!days.length) return []
  const first = new Date(`${days[0].date}T00:00:00Z`)
  const padded = [...Array((first.getUTCDay() + 6) % 7).fill(null), ...days]
  const weeks = []
  for (let i = 0; i < padded.length; i += 7) {
    const week = padded.slice(i, i + 7)
    while (week.length < 7) week.push(null)
    weeks.push(week)
  }
  return weeks
}
