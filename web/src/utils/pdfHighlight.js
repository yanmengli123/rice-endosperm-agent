/**
 * PDF 坐标 → 视口坐标换算（纯函数，可脱离 pdf.js 单测）。
 *
 * 证据 fragments 的 bbox（coordinate_space=pdf_points）单位为 pt，**原点在页面
 * 左上、y 向下**（MinerU/PyMuPDF 约定，DTO 以 origin="top_left" 显式声明）。
 * 换算时先转到 PDF user space（原点左下、y 向上），再套 pdf.js PageViewport
 * 的同一组矩阵，保证与文本层/画布几何一致；输出为 CSS 像素矩形。
 *
 * 变换公式与 pdf.js PageViewport 一致（rotation ∈ {0,90,180,270}，顺时针；
 * view 为页面 view box，pt）：
 *   rotation 0:   [ s, 0, 0, -s, -s*x0,  s*y1 ]   vx=s*(x-x0) vy=s*(y1-y)
 *   rotation 90:  [ 0, s, s,  0, -s*y0, -s*x0 ]   vx=s*(y-y0) vy=s*(x-x0)
 *   rotation 180: [-s, 0, 0,  s,  s*x1, -s*y0 ]   vx=s*(x1-x) vy=s*(y-y0)
 *   rotation 270: [ 0,-s,-s,  0,  s*y0,  s*x1 ]   vx=s*(y1-y) vy=s*(x1-x)
 */

export const PDF_HIGHLIGHT_VERSION = '1.2'

export const ORIGIN_TOP_LEFT = 'top_left'
export const ORIGIN_BOTTOM_LEFT = 'bottom_left'

const normalizeRotation = (rotation) => {
  const value = (((Number(rotation) || 0) % 360) + 360) % 360
  return [0, 90, 180, 270].includes(value) ? value : 0
}

const transformFor = (rotation, scale, view) => {
  const { x0, y0, x1, y1 } = view
  const s = scale
  switch (rotation) {
    case 90:
      return [0, s, s, 0, -s * y0, -s * x0]
    case 180:
      return [-s, 0, 0, s, s * x1, -s * y0]
    case 270:
      return [0, -s, -s, 0, s * y1, s * x1]
    case 0:
    default:
      return [s, 0, 0, -s, -s * x0, s * y1]
  }
}

const applyTransform = (transform, x, y) => {
  const [a, b, c, d, e, f] = transform
  return { x: a * x + c * y + e, y: b * x + d * y + f }
}

/**
 * 页面视口尺寸（pdf.js viewport.width/height 的等价物）。
 * @param {{x0:number,y0:number,x1:number,y1:number}} view 页面 view box（pt）
 * @param {number} scale
 * @param {number} rotation
 */
export const pageViewportSize = (view, scale, rotation = 0) => {
  const rot = normalizeRotation(rotation)
  const pw = (view.x1 - view.x0) * scale
  const ph = (view.y1 - view.y0) * scale
  return rot === 90 || rot === 270 ? { width: ph, height: pw } : { width: pw, height: ph }
}

/**
 * bbox [x0,y0,x1,y1] → 画布矩形 {left,top,width,height}。
 * origin 默认 top_left（证据 fragments 的实际约定）；传 bottom_left 表示 bbox 已是
 * PDF user space。传入非法 bbox 返回 null（调用方应跳过高亮而不是画出错误矩形）。
 */
export const pdfRectToScreen = (
  bbox,
  { view, scale = 1, rotation = 0, origin = ORIGIN_TOP_LEFT } = {}
) => {
  if (!Array.isArray(bbox) || bbox.length !== 4) return null
  let numbers = bbox.map((value) => Number(value))
  if (numbers.some((value) => !Number.isFinite(value))) return null
  if (!view || [view.x0, view.y0, view.x1, view.y1].some((value) => !Number.isFinite(value)))
    return null
  if (origin !== ORIGIN_BOTTOM_LEFT) {
    // 左上原点 → PDF user space：x 加 view 偏移，y 从页顶距离改为页底距离
    const [x0, top, x1, bottom] = numbers
    numbers = [view.x0 + x0, view.y1 - bottom, view.x0 + x1, view.y1 - top]
  }

  const rot = normalizeRotation(rotation)
  const transform = transformFor(rot, scale, view)
  // 矩形变换后取四角包围盒（y 翻转/旋转下 min/max 才正确）
  const corners = [
    applyTransform(transform, numbers[0], numbers[1]),
    applyTransform(transform, numbers[0], numbers[3]),
    applyTransform(transform, numbers[2], numbers[1]),
    applyTransform(transform, numbers[2], numbers[3])
  ]
  const xs = corners.map((point) => point.x)
  const ys = corners.map((point) => point.y)
  const left = Math.min(...xs)
  const right = Math.max(...xs)
  // 画布 y 轴向下为正：top 取 min(y)
  const top = Math.min(...ys)
  const bottom = Math.max(...ys)
  const rect = { left, top, width: right - left, height: bottom - top }
  if (rect.width <= 0 || rect.height <= 0) return null
  return rect
}

/**
 * 从证据 DTO 提取高亮输入（首个 fragment）；无有效 fragment 返回 null。
 */
export const evidenceHighlight = (evidence) => {
  const fragment = evidence?.locator?.fragments?.[0]
  if (!fragment || !Array.isArray(fragment.bbox)) return null
  const page = Number(fragment.page_number) || Number(fragment.page_index) + 1 || 1
  return {
    bbox: fragment.bbox,
    page,
    coordinateSpace: fragment.coordinate_space || 'pdf_points',
    origin: fragment.origin || ORIGIN_TOP_LEFT
  }
}

// ---------------------------------------------------------------------------
// 文本层句子级定位：服务端只给出"哪一句"（locator.highlight.quote），几何由
// pdf.js getTextContent() 的 items 在客户端确定性推导；任何一步失败返回 null，
// 调用方回退块级 bbox——绝不画猜测矩形。
// ---------------------------------------------------------------------------

const MIN_MATCH_CHARS = 8
const EDGE_ANCHOR_CHARS = 40
const MAX_OCCURRENCES = 8
// 约束框外扩：图注紧贴图像块下方、段落续行紧贴块边界时仍视为"属于该块"
const CONSTRAINT_MARGIN_LINES = 4

// MinerU 会把上标/希腊字母写成 LaTeX（"$^{1,2,\dagger}$"），文本层里则是纯字符
const TEX_SYMBOLS = {
  dagger: '†',
  ddagger: '‡',
  times: '×',
  pm: '±',
  mu: 'μ',
  alpha: 'α',
  beta: 'β',
  gamma: 'γ',
  delta: 'δ',
  Delta: 'Δ',
  le: '≤',
  leq: '≤',
  ge: '≥',
  geq: '≥',
  circ: '°',
  degree: '°',
  sim: '~',
  cdot: '·',
  prime: "'"
}

/**
 * 文本归一化：NFKC 折叠连字（ﬁ→fi）、短横/引号变体统一、去软连字符，
 * LaTeX 命令映射为字符并剥掉 $ ^ _ {} 等标记，最后去全部空白与连字符
 * （行末断词 "transcrip-tional" 与原文一致）并小写。两侧同一函数处理，比较才有意义。
 */
export const foldTextForMatch = (text) =>
  String(text || '')
    .normalize('NFKC')
    .replace(/[\u2010-\u2015\u2212]/g, '-')
    .replace(/[\u2018\u2019\u201a\u2032]/g, "'")
    .replace(/[\u201c\u201d\u201e\u2033]/g, '"')
    .replace(/\u00ad/g, '')
    .replace(/\\([A-Za-z]+)/g, (_match, name) => TEX_SYMBOLS[name] ?? '')
    .replace(/[$^{}\\_]/g, '')
    .toLowerCase()
    .replace(/[\s-]+/g, '')

// pdf.js Util.transform 的等价实现（m1 × m2），避免纯函数依赖 pdfjs 运行时
const composeTransform = (m1, m2) => [
  m1[0] * m2[0] + m1[2] * m2[1],
  m1[1] * m2[0] + m1[3] * m2[1],
  m1[0] * m2[2] + m1[2] * m2[3],
  m1[1] * m2[2] + m1[3] * m2[3],
  m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
  m1[1] * m2[4] + m1[3] * m2[5] + m1[5]
]

const rectsIntersect = (a, b) =>
  a.left < b.left + b.width &&
  a.left + a.width > b.left &&
  a.top < b.top + b.height &&
  a.top + a.height > b.top

const unionRect = (rects) => {
  const left = Math.min(...rects.map((r) => r.left))
  const top = Math.min(...rects.map((r) => r.top))
  const right = Math.max(...rects.map((r) => r.left + r.width))
  const bottom = Math.max(...rects.map((r) => r.top + r.height))
  return { left, top, width: right - left, height: bottom - top }
}

/**
 * 单个文本项 → 视口矩形（仅处理未旋转文本；旋转文本返回 null 由调用方跳过）。
 * item.width 为 PDF user space 宽度（pdf.js 文档："Width in device space"），
 * rotation 0 视口下 x 方向只有 scale 一个缩放因子。
 */
const textItemRect = (item, viewport) => {
  if (!Array.isArray(item?.transform) || item.transform.length !== 6) return null
  const tx = composeTransform(viewport.transform, item.transform)
  if (Math.abs(Math.atan2(tx[1], tx[0])) > 0.01) return null
  const fontHeight = Math.hypot(tx[2], tx[3])
  const width = (Number(item.width) || 0) * viewport.scale
  if (!(fontHeight > 0) || !(width > 0)) return null
  // 字形盒：基线上方 1 个字高 + 下方 0.2 字高容纳降部
  return { left: tx[4], top: tx[5] - fontHeight, width, height: fontHeight * 1.2 }
}

// 按阅读顺序聚行（rects 来自文本项顺序）：同一基线附近且水平相邻的项并入同一行，
// 返回顺序 = 句子的阅读顺序，首行即句子起点（跨栏续行也能正确排在其后）。
// 水平间隙上限 1.5 字高：词间距（含两端对齐拉伸）在此之内，栏间距在此之外。
const clusterIntoLines = (rects) => {
  const lines = []
  for (const rect of rects) {
    const line = lines.find(
      (candidate) =>
        Math.abs(rect.top - candidate.anchorTop) < rect.height * 0.5 &&
        rect.left >= candidate.right - rect.height * 2 &&
        rect.left <= candidate.right + rect.height * 1.5
    )
    if (line) {
      line.members.push(rect)
      line.right = Math.max(line.right, rect.left + rect.width)
    } else {
      lines.push({ anchorTop: rect.top, right: rect.left + rect.width, members: [rect] })
    }
  }
  return lines.map((line) => unionRect(line.members))
}

const findOccurrences = (haystack, needle) => {
  const positions = []
  let from = 0
  while (positions.length < MAX_OCCURRENCES) {
    const index = haystack.indexOf(needle, from)
    if (index < 0) break
    positions.push(index)
    from = index + 1
  }
  return positions
}

/**
 * 在归一化文本中定位 needle：优先整句；失败则用首尾各 40 字符做前后锚
 * （对齐服务端 prefix/suffix selector 思想），仅首锚命中时返回部分区间。
 */
const locateNeedle = (haystack, needle) => {
  const full = findOccurrences(haystack, needle)
  if (full.length)
    return { ranges: full.map((start) => [start, start + needle.length]), partial: false }
  if (needle.length <= EDGE_ANCHOR_CHARS * 2) return null
  const head = needle.slice(0, EDGE_ANCHOR_CHARS)
  const tail = needle.slice(-EDGE_ANCHOR_CHARS)
  const heads = findOccurrences(haystack, head)
  if (!heads.length) return null
  const ranges = []
  for (const start of heads) {
    const tailIndex = haystack.indexOf(tail, start + head.length)
    const withinReach = tailIndex >= 0 && tailIndex - start <= needle.length * 1.5
    ranges.push(withinReach ? [start, tailIndex + tail.length] : [start, start + head.length])
  }
  return { ranges, partial: true }
}

/**
 * 文本层引文匹配主入口。
 *
 * @param {{items: Array}} textContent  pdf.js page.getTextContent() 结果
 * @param {{transform:number[], scale:number, rotation:number}} viewport pdf.js PageViewport
 * @param {string} query 服务端精化句（locator.highlight.quote）
 * @param {{left,top,width,height}|null} constraintRect 块级 bbox 的视口矩形，用于消歧：
 *        优先取与之相交（或外扩几行内）的命中；都不相交时仅当页内命中唯一才接受
 *        （图注在图像块下方、段落续行在下一栏都是合法的"框外"命中）
 * @returns {{rects: Array, partial: boolean, withinConstraint: boolean}|null}
 */
export const computeTextLayerHighlight = ({
  textContent,
  viewport,
  query,
  constraintRect = null
}) => {
  const items = Array.isArray(textContent?.items) ? textContent.items : []
  if (!items.length || !viewport || !Array.isArray(viewport.transform)) return null
  if (normalizeRotation(viewport.rotation) !== 0) return null
  const needle = foldTextForMatch(query)
  if (needle.length < MIN_MATCH_CHARS) return null

  // 归一化字符 → (item 序号, 该 item 内的归一化字符偏移, item 归一化长度)
  const owners = []
  const offsets = []
  const foldedLengths = []
  const pieces = []
  items.forEach((item, index) => {
    const folded = foldTextForMatch(item.str)
    foldedLengths.push(folded.length)
    pieces.push(folded)
    for (let offset = 0; offset < folded.length; offset += 1) {
      owners.push(index)
      offsets.push(offset)
    }
  })
  const located = locateNeedle(pieces.join(''), needle)
  if (!located) return null

  const candidates = []
  for (const [start, end] of located.ranges) {
    const rects = []
    const firstItem = owners[start]
    const lastItem = owners[end - 1]
    if (firstItem === undefined || lastItem === undefined) continue
    for (let index = firstItem; index <= lastItem; index += 1) {
      const rect = textItemRect(items[index], viewport)
      if (!rect) continue
      const length = foldedLengths[index]
      // 首尾项按字符比例裁剪，避免"整行一个 item"的 PDF 把整行都高亮
      const fromRatio = index === firstItem ? offsets[start] / length : 0
      const toRatio = index === lastItem ? (offsets[end - 1] + 1) / length : 1
      const clipped = {
        left: rect.left + rect.width * fromRatio,
        top: rect.top,
        width: rect.width * (toRatio - fromRatio),
        height: rect.height
      }
      if (clipped.width > 0) rects.push(clipped)
    }
    if (rects.length) candidates.push(clusterIntoLines(rects))
  }
  if (!candidates.length) return null

  const finish = (lines, withinConstraint) => ({
    rects: lines,
    partial: located.partial,
    withinConstraint
  })
  if (!constraintRect) return finish(candidates[0], true)
  const inside = candidates.find((lines) => rectsIntersect(unionRect(lines), constraintRect))
  if (inside) return finish(inside, true)
  const lineHeight = Math.max(...candidates[0].map((line) => line.height))
  const margin = lineHeight * CONSTRAINT_MARGIN_LINES
  const expanded = {
    left: constraintRect.left - margin,
    top: constraintRect.top - margin,
    width: constraintRect.width + margin * 2,
    height: constraintRect.height + margin * 2
  }
  const nearby = candidates.find((lines) => rectsIntersect(unionRect(lines), expanded))
  if (nearby) return finish(nearby, false)
  // 页内唯一命中即使远离块级框也是无歧义的；多处命中且都不沾边才放弃
  return candidates.length === 1 ? finish(candidates[0], false) : null
}
