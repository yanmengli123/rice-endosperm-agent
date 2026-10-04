export function canvasPoint(clientX, clientY, rect, view = { x: 0, y: 0, scale: 1 }) {
  if (!rect.width || !rect.height || !Number.isFinite(view.scale) || view.scale <= 0) return null
  return {
    x: (((clientX - rect.left) * 900) / rect.width - view.x) / view.scale,
    y: (((clientY - rect.top) * 560) / rect.height - view.y) / view.scale
  }
}

export function graphCameraPosition(direction) {
  return (
    { front: [0, 0, 330], top: [0, 330, 0.01], side: [330, 0, 0], iso: [220, 220, 220] }[
      direction
    ] || [0, 75, 310]
  )
}

export function graphGeometry(nodes, algorithm = 'circular', spacing = 1) {
  const count = nodes.length,
    distance = 40 * Math.max(0.25, Math.min(5, Number(spacing) || 1))
  const columns = Math.ceil(Math.sqrt(count))
  const radius = Math.max(distance, Math.sqrt(count) * distance)
  const hash = (text) => {
    let value = 2166136261
    for (const char of text) value = Math.imul(value ^ char.charCodeAt(0), 16777619)
    return (value >>> 0) / 4294967296
  }
  return nodes.map((node, index) => {
    if (algorithm === 'grid')
      return {
        ...node,
        x: 450 + ((index % columns) - (columns - 1) / 2) * distance,
        y: 280 + (Math.floor(index / columns) - (Math.ceil(count / columns) - 1) / 2) * distance
      }
    if (algorithm === 'random')
      return {
        ...node,
        x: 450 + (hash(`${node.rid}:x`) - 0.5) * radius * 2,
        y: 280 + (hash(`${node.rid}:y`) - 0.5) * radius * 2
      }
    const angle = (2 * Math.PI * index) / Math.max(1, count)
    return { ...node, x: 450 + radius * Math.cos(angle), y: 280 + radius * Math.sin(angle) }
  })
}

export const graphStyleDefaults = () => ({
  colorMode: 'quartile',
  palette: 'viridis',
  sizeBase: 1,
  scaleAlgo: 'sqrt',
  labelMode: 'selected',
  showEdges: true,
  showEdgeDir: false,
  edgeWidth: 1,
  edgeColor: '',
  background: 'theme',
  bgColor: '#ffffff'
})

export function graphNodeRadius(node, style = {}) {
  const value = Math.max(0, Number(node.citations) || 0)
  const scaled =
    {
      log: Math.log2(1 + value),
      sqrt: Math.sqrt(value),
      pow06: value ** 0.6,
      linear: value,
      uniform: 1
    }[style.scaleAlgo || 'sqrt'] ?? Math.sqrt(value)
  return Math.min(
    40,
    (4 + Math.min(14, scaled * 2.2)) * Math.max(0.25, Math.min(4, Number(style.sizeBase) || 1))
  )
}

export function graphNodeColor(node, style = {}, dark = false) {
  if (!style.colorMode || style.colorMode === 'quartile')
    return (
      (dark
        ? { Q1: '#69b1ff', Q2: '#95de64', Q3: '#ffd666', Q4: '#a6a6a6' }
        : { Q1: '#1677ff', Q2: '#52c41a', Q3: '#faad14', Q4: '#8c8c8c' })[node.quartile] ||
      (dark ? '#5a5a5a' : '#d9d9d9')
    )
  if (style.colorMode === 'cluster') {
    if (node.cocitation_cluster == null) return dark ? '#5a5a5a' : '#d9d9d9'
    return ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7'][
      Math.abs(Number(node.cocitation_cluster) || 0) % 8
    ]
  }
  const raw =
    style.colorMode === 'year'
      ? node.year
      : style.colorMode === 'citations'
        ? node.citations
        : node.impact_factor
  if (raw == null) return dark ? '#5a5a5a' : '#d9d9d9'
  const t = Math.max(
    0,
    Math.min(
      1,
      style.colorMode === 'year'
        ? (raw - 1990) / 40
        : Math.log2(1 + Math.max(0, Number(raw) || 0)) / 6
    )
  )
  const colors = {
    viridis: ['#440154', '#21918c', '#fde725'],
    blues: ['#deebf7', '#6baed6', '#08519c'],
    warm: ['#fff7bc', '#fec44f', '#d95f0e']
  }[style.palette] || ['#440154', '#21918c', '#fde725']
  const index = Math.min(1, Math.floor(t * 2)),
    fraction = t * 2 - index
  return (
    '#' +
    [1, 3, 5]
      .map((offset) => {
        const a = parseInt(colors[index].slice(offset, offset + 2), 16)
        const b = parseInt(colors[index + 1].slice(offset, offset + 2), 16)
        return Math.round(a + (b - a) * fraction)
          .toString(16)
          .padStart(2, '0')
      })
      .join('')
  )
}

export function graphNodeLabel(node, mode, selectedRid) {
  if (mode === 'none' || (mode === 'selected' && node.rid !== selectedRid)) return ''
  if (mode === 'rid') return node.rid
  if (mode === 'year_cited') return `${node.year || '未知年'} · ${node.citations || 0}`
  return (node.title || node.rid).slice(0, 48)
}
