/**
 * 知识图谱视觉身份（跨视图共享的唯一事实源）
 *
 * 类型调色盘、label 哈希取色、节点/边可视类型提取与 Chunk 过滤谓词。
 * 画布（GraphCanvas）与洞察面板（GraphInsightPanel）必须从同一处取色，
 * 保证同一实体类型在力导向图与弦图中颜色一致。
 *
 * 纯 JS、零依赖（不含 DOM/Vue），可被裸 node spec 直接 import。
 */

export const CHUNK_NODE_LABEL = 'Chunk'
export const CHUNK_NODE_COLOR = '#8c8c8c'
export const CHUNK_MENTION_EDGE_LABEL = 'MENTIONS'

export const NODE_LABEL_COLORS = [
  '#3996ae',
  '#5ad8a6',
  '#f6bd16',
  '#f27c7c',
  '#9581cc',
  '#6dc8ec',
  '#ff9d4d',
  '#92d050',
  '#e885ba'
]

export const EDGE_LABEL_COLORS = [
  '#99add1',
  '#3996ae',
  '#13c2c2',
  '#faad14',
  '#f27c7c',
  '#9581cc',
  '#52c41a',
  '#ff9d4d'
]

export function hashString(value) {
  let hash = 0
  for (let i = 0; i < value.length; i++) {
    hash = (hash << 5) - hash + value.charCodeAt(i)
    hash |= 0
  }
  return Math.abs(hash)
}

export function getPaletteColor(label, colors) {
  return colors[hashString(label) % colors.length]
}

export function getNodeVisualLabel(node) {
  return node?.type || node?.normalized?.type || node?.properties?.label || 'Entity'
}

export function getNodeColor(node) {
  const label = getNodeVisualLabel(node)
  if (!isEntityNode(node)) return CHUNK_NODE_COLOR
  return getPaletteColor(label, NODE_LABEL_COLORS)
}

export function getEdgeVisualLabel(edge) {
  return edge?.type || edge?.normalized?.type || 'RELATED_TO'
}

export function getEdgeColor(edge) {
  return getPaletteColor(getEdgeVisualLabel(edge), EDGE_LABEL_COLORS)
}

export function isEntityNode(node) {
  if (node?.node_kind) return node.node_kind === 'entity'
  if (node?.labels?.includes('Chunk')) return false
  if (node?.labels?.includes('Entity')) return true
  return getNodeVisualLabel(node) !== CHUNK_NODE_LABEL
}

export function isEntityRelationEdge(edge) {
  if (edge?.edge_kind) return edge.edge_kind === 'relation'
  if (edge?.relationship_type) return edge.relationship_type !== CHUNK_MENTION_EDGE_LABEL
  return getEdgeVisualLabel(edge) !== CHUNK_MENTION_EDGE_LABEL
}

// ---------------------------------------------------------------------------
// 暗色主题自适应提亮：调色盘里最深的身份色（如 #3996ae，L≈45%）在近黑面板底上
// 即使全不透明对比度也只有 ~3.8:1，细形状（弦带）不可辨。与 --chart-palette-*
// 在暗色下换亮变体同一思路：保持色相（类型身份可辨），把亮度抬到下限。
// ---------------------------------------------------------------------------

function hexToRgb(hex) {
  const value = hex.replace('#', '')
  return [
    parseInt(value.slice(0, 2), 16) / 255,
    parseInt(value.slice(2, 4), 16) / 255,
    parseInt(value.slice(4, 6), 16) / 255
  ]
}

function rgbToHsl([r, g, b]) {
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  if (max === min) return [0, 0, l]
  const d = max - min
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
  const h =
    max === r
      ? ((g - b) / d + (g < b ? 6 : 0)) * 60
      : max === g
        ? ((b - r) / d + 2) * 60
        : ((r - g) / d + 4) * 60
  return [h, s, l]
}

function hslToRgb([h, s, l]) {
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = l - c / 2
  const [r, g, b] =
    h < 60
      ? [c, x, 0]
      : h < 120
        ? [x, c, 0]
        : h < 180
          ? [0, c, x]
          : h < 240
            ? [0, x, c]
            : h < 300
              ? [x, 0, c]
              : [c, 0, x]
  return [r + m, g + m, b + m]
}

/**
 * 暗色主题下提亮颜色：亮度低于 minLightness 时抬到下限，色相与饱和度不变；
 * 已达亮度的颜色原样返回（幂等）。
 *
 * @param {string} hex #rrggbb
 * @param {number} [minLightness=0.62]
 * @returns {string} #rrggbb
 */
export function lightenForDarkTheme(hex, minLightness = 0.62) {
  const hsl = rgbToHsl(hexToRgb(hex))
  if (hsl[2] >= minLightness) return hex
  const [r, g, b] = hslToRgb([hsl[0], hsl[1], minLightness])
  const toHex = (v) =>
    Math.round(Math.min(1, Math.max(0, v)) * 255)
      .toString(16)
      .padStart(2, '0')
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`
}
