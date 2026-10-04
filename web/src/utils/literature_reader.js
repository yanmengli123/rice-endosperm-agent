export const READER_TABS = [
  'original',
  'pdf',
  'parallel',
  'translation',
  'images',
  'noteL1',
  'noteL2',
  'noteL3'
]
export const normalizeReaderPreferences = (raw) => ({
  tab: READER_TABS.includes(raw?.tab) ? raw.tab : 'original',
  fontSize: [14, 16, 18, 20, 22, 24].includes(raw?.fontSize) ? raw.fontSize : 16,
  pdfScale: Number.isFinite(raw?.pdfScale) ? Math.min(4, Math.max(0.25, raw.pdfScale)) : 1,
  pdfFitWidth: raw?.pdfFitWidth !== false,
  autoHideMissing: raw?.autoHideMissing !== false,
  tabOrder: [
    ...new Set([...(Array.isArray(raw?.tabOrder) ? raw.tabOrder : []), ...READER_TABS])
  ].filter((key) => READER_TABS.includes(key)),
  hiddenTabs: [...new Set(Array.isArray(raw?.hiddenTabs) ? raw.hiddenTabs : [])].filter(
    (key) => key !== 'original' && READER_TABS.includes(key)
  )
})
export const moveReaderTab = (order, from, to) => {
  const result = normalizeReaderPreferences({ tabOrder: order }).tabOrder
  const source = result.indexOf(from),
    target = result.indexOf(to)
  if (source < 0 || target < 0) return result
  result.splice(source, 1)
  result.splice(target, 0, from)
  return result
}
export const readReaderPreferences = (storage, uid) => {
  try {
    return normalizeReaderPreferences(
      uid ? JSON.parse(storage.getItem(`yuxi:literature:reader:${uid}`)) : null
    )
  } catch {
    return normalizeReaderPreferences(null)
  }
}
export const writeReaderPreferences = (storage, uid, value) => {
  if (!uid) return
  try {
    storage.setItem(
      `yuxi:literature:reader:${uid}`,
      JSON.stringify(normalizeReaderPreferences(value))
    )
  } catch {
    /* browser storage can be denied; preferences never block reading */
  }
}
export const parseLiteratureLink = (target) => {
  const value = String(target || '').trim()
  if (
    !value ||
    value.length > 500 ||
    (/^(?:[a-z]+:|\/|\\)/i.test(value) && !/^10\.\d{4,9}\//.test(value))
  )
    return ''
  if (value.replaceAll('\\', '/').split('/').includes('..')) return ''
  const withoutAnchor = value.split('#')[0].replace(/\.md$/i, '')
  return withoutAnchor.replace(
    /(?:_L[123](?:_(?:note|deep|relations))?|_(?:note|deep|relations))$/i,
    ''
  )
}

// Match rendered headings without interpolating user text into DOM selectors.
export const normalizeReaderAnchor = (value) => {
  let text = String(value || '').replace(/^#/, '')
  try {
    text = decodeURIComponent(text)
  } catch {
    /* malformed escapes remain literal */
  }
  return text
    .normalize('NFKC')
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\p{M}\s_^-]/gu, '')
    .replace(/\s+/g, '-')
}

export const locateParallelAnchor = (rows, anchor) => {
  const wanted = normalizeReaderAnchor(anchor)
  if (!wanted) return null
  const counts = new Map(),
    matches = []
  rows.forEach((row, index) => {
    const localCounts = new Map()
    for (const line of String(row.original || '').split('\n')) {
      if (wanted.startsWith('^')) {
        if (line.trim().endsWith(wanted)) matches.push({ index, anchor: wanted })
        continue
      }
      const heading = /^#{1,6}\s+(.+?)\s*#*\s*$/.exec(line)
      if (!heading) continue
      const key = normalizeReaderAnchor(heading[1]),
        count = counts.get(key) || 0,
        local = localCounts.get(key) || 0
      counts.set(key, count + 1)
      localCounts.set(key, local + 1)
      if (wanted === (count ? `${key}-${count}` : key))
        matches.push({ index, anchor: local ? `${key}-${local}` : key })
    }
  })
  return matches.length === 1 ? matches[0] : null
}
