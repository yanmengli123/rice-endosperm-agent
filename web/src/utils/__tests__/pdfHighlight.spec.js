// pdfHighlight 坐标换算单测：node src/utils/__tests__/pdfHighlight.spec.js
import assert from 'node:assert/strict'
import {
  computeTextLayerHighlight,
  evidenceHighlight,
  foldTextForMatch,
  pageViewportSize,
  pdfRectToScreen
} from '../pdfHighlight.js'

// 标准 Letter 页面 view box（pt），view 高 792、宽 612
const VIEW = { x0: 0, y0: 0, x1: 612, y1: 792 }
const SCALE = 2

// 1a. 默认 origin=top_left（MinerU/PyMuPDF 约定）：y 是到页顶的距离，不翻转。
//     真实锚点 [40.48, 586.55, 281.57, 752.85] 位于第 3 页左栏底部（与文本层一致）
{
  const rect = pdfRectToScreen([40.48, 586.55, 281.57, 752.85], {
    view: VIEW,
    scale: SCALE,
    rotation: 0
  })
  assert.equal(rect.left, 40.48 * SCALE)
  assert.equal(rect.top, 586.55 * SCALE)
  assert.equal(rect.width, (281.57 - 40.48) * SCALE)
  assert.ok(Math.abs(rect.height - (752.85 - 586.55) * SCALE) < 1e-9)
}

// 1b. origin=bottom_left（PDF user space）：y 翻转——y 接近 y1 映射到画布顶部
{
  const rect = pdfRectToScreen([40.48, 586.55, 281.57, 752.85], {
    view: VIEW,
    scale: SCALE,
    rotation: 0,
    origin: 'bottom_left'
  })
  assert.equal(rect.left, 40.48 * SCALE)
  assert.equal(rect.top, (VIEW.y1 - 752.85) * SCALE)
  assert.ok(Math.abs(rect.height - (752.85 - 586.55) * SCALE) < 1e-9)
}

// 1c. 两种原点在同一页面上互为镜像：top_left 的 top + bottom_left 的 bottom = 页高
{
  const topLeft = pdfRectToScreen([10, 100, 50, 150], { view: VIEW, scale: 1 })
  const bottomLeft = pdfRectToScreen([10, 100, 50, 150], {
    view: VIEW,
    scale: 1,
    origin: 'bottom_left'
  })
  assert.equal(topLeft.top + (bottomLeft.top + bottomLeft.height), VIEW.y1)
}

// 2. 视口尺寸：0/180 不翻转宽高，90/270 交换
{
  const portrait = pageViewportSize(VIEW, SCALE, 0)
  assert.equal(portrait.width, 612 * SCALE)
  assert.equal(portrait.height, 792 * SCALE)
  const landscape = pageViewportSize(VIEW, SCALE, 90)
  assert.equal(landscape.width, 792 * SCALE)
  assert.equal(landscape.height, 612 * SCALE)
  assert.deepEqual(pageViewportSize(VIEW, SCALE, 180), portrait)
  assert.deepEqual(pageViewportSize(VIEW, SCALE, 270), landscape)
}

// 3. rotation 180：水平翻转,包围盒仍完整覆盖
{
  const rect = pdfRectToScreen([100, 700, 200, 750], {
    view: VIEW,
    scale: SCALE,
    rotation: 180,
    origin: 'bottom_left'
  })
  assert.equal(rect.left, (VIEW.x1 - 200) * SCALE)
  assert.equal(rect.width, 100 * SCALE)
  assert.equal(rect.top, 700 * SCALE)
  assert.equal(rect.height, 50 * SCALE)
}

// 4. rotation 90：x/y 交换
{
  const rect = pdfRectToScreen([100, 700, 200, 750], {
    view: VIEW,
    scale: SCALE,
    rotation: 90,
    origin: 'bottom_left'
  })
  assert.equal(rect.left, (700 - VIEW.y0) * SCALE)
  assert.equal(rect.width, 50 * SCALE)
  assert.equal(rect.height, 100 * SCALE)
}

// 5. 非法输入返回 null（不画错误矩形）
{
  assert.equal(pdfRectToScreen(null, { view: VIEW, scale: SCALE }), null)
  assert.equal(pdfRectToScreen([1, 2, 3], { view: VIEW, scale: SCALE }), null)
  assert.equal(pdfRectToScreen(['a', 2, 3, 4], { view: VIEW, scale: SCALE }), null)
  assert.equal(pdfRectToScreen([0, 0, 10, 20], { view: null, scale: SCALE }), null)
  // 倒置 bbox(normalized rect 写法)→ 按 min/max 归一化,不拒绝
  const inverted = pdfRectToScreen([100, 100, 50, 200], {
    view: VIEW,
    scale: SCALE,
    rotation: 0,
    origin: 'bottom_left'
  })
  assert.equal(inverted.left, 100)
  assert.equal(inverted.top, (VIEW.y1 - 200) * SCALE)
  assert.equal(inverted.width, 50 * SCALE)
  assert.equal(inverted.height, 100 * SCALE)
  // 零面积(退化为点)→ null
  assert.equal(pdfRectToScreen([10, 10, 10, 10], { view: VIEW, scale: SCALE, rotation: 0 }), null)
}

// 6. evidenceHighlight 提取与默认页码
{
  const hit = evidenceHighlight({
    locator: {
      fragments: [
        { page_index: 2, page_number: 3, bbox: [1, 2, 3, 4], coordinate_space: 'pdf_points' }
      ]
    }
  })
  assert.deepEqual(hit, {
    bbox: [1, 2, 3, 4],
    page: 3,
    coordinateSpace: 'pdf_points',
    origin: 'top_left'
  })
  const byIndex = evidenceHighlight({
    locator: { fragments: [{ page_index: 2, bbox: [1, 2, 3, 4] }] }
  })
  assert.equal(byIndex.page, 3)
  assert.equal(evidenceHighlight({ locator: { fragments: [] } }), null)
  assert.equal(evidenceHighlight(null), null)
}

// ---------------------------------------------------------------------------
// 文本层句子级定位（computeTextLayerHighlight）
// ---------------------------------------------------------------------------

// 合成 rotation 0 视口：pdf.js PageViewport.transform = [s,0,0,-s,0,s*height]
const makeViewport = (scale, rotation = 0) => ({
  scale,
  rotation,
  transform: [scale, 0, 0, -scale, 0, scale * VIEW.y1]
})
// 文本项：基线起点 (x,y)（PDF user space），字号 f，宽度 w（user space）
const makeItem = (str, x, y, w, f = 10) => ({ str, width: w, transform: [f, 0, 0, f, x, y] })
const FONT = 10
const PAGE_ITEMS = [
  makeItem('The structure of ', 40, 700, 80),
  makeItem('OsMYB73 protein was ', 120, 700, 100),
  makeItem('also predicted', 220, 700, 60),
  makeItem('and the results revealed two SANT domains.', 40, 688, 200),
  makeItem('Unrelated text far below the paragraph.', 40, 300, 220)
]
const SENTENCE =
  'The structure of OsMYB73 protein was also predicted and the results revealed two SANT domains.'

// 5. 归一化：连字/短横变体/空白/软连字符/大小写/LaTeX 标记全部折叠
{
  assert.equal(foldTextForMatch('115–164 and ﬁgure S1'), '115164andfigures1')
  assert.equal(foldTextForMatch('transcrip-\ntional  Regulation'), 'transcriptionalregulation')
  assert.equal(foldTextForMatch('“quoted” ‘text’'), '"quoted"' + "'" + 'text' + "'")
  // MinerU 的 LaTeX 上标/符号与文本层纯字符归一到同一形态
  assert.equal(foldTextForMatch('Song Liu $^{1,2,\\dagger}$'), foldTextForMatch('Song Liu 1,2,†'))
  assert.equal(foldTextForMatch('LOC_Os01g63680 (1 $\\times$ 10$^{-3}$)'), 'locos01g63680(1×103)')
}

// 6. 整句命中：跨 4 个 item、两行，返回两条逐行矩形（几何与 pdf.js 变换一致）
{
  const viewport = makeViewport(SCALE)
  const hit = computeTextLayerHighlight({
    textContent: { items: PAGE_ITEMS },
    viewport,
    query: SENTENCE
  })
  assert.ok(hit)
  assert.equal(hit.partial, false)
  assert.equal(hit.rects.length, 2)
  const fontHeight = FONT * SCALE
  // 第一行：left=40*s，right=(220+60)*s，top=s*(792-700)-fontHeight
  assert.equal(hit.rects[0].left, 40 * SCALE)
  assert.equal(hit.rects[0].width, (280 - 40) * SCALE)
  assert.equal(hit.rects[0].top, (VIEW.y1 - 700) * SCALE - fontHeight)
  assert.ok(Math.abs(hit.rects[0].height - fontHeight * 1.2) < 1e-9)
  // 第二行
  assert.equal(hit.rects[1].left, 40 * SCALE)
  assert.equal(hit.rects[1].width, 200 * SCALE)
  assert.equal(hit.rects[1].top, (VIEW.y1 - 688) * SCALE - fontHeight)
}

// 7. 约束矩形只用于消歧：块内命中优先；页内唯一命中即使在块外也接受（图注/跨栏续行）；
//    同页多处命中时取与块相交的那一处，都不相交才放弃
{
  const viewport = makeViewport(SCALE)
  const paragraphBox = pdfRectToScreen([40, 75, 290, 110], { view: VIEW, scale: SCALE })
  const inside = computeTextLayerHighlight({
    textContent: { items: PAGE_ITEMS },
    viewport,
    query: SENTENCE,
    constraintRect: paragraphBox
  })
  assert.ok(inside)
  assert.equal(inside.withinConstraint, true)
  const elsewhere = pdfRectToScreen([40, 600, 290, 700], { view: VIEW, scale: SCALE })
  const unique = computeTextLayerHighlight({
    textContent: { items: PAGE_ITEMS },
    viewport,
    query: SENTENCE,
    constraintRect: elsewhere
  })
  assert.ok(unique)
  assert.equal(unique.withinConstraint, false)

  const repeated = 'Repeated sentence appears twice on this page.'
  const twice = [makeItem(repeated, 40, 700, 200), makeItem(repeated, 40, 300, 200)]
  // 约束框在第二处（user y=300 → 距页顶 492）
  const aroundSecond = pdfRectToScreen([30, 480, 300, 500], { view: VIEW, scale: SCALE })
  const picked = computeTextLayerHighlight({
    textContent: { items: twice },
    viewport,
    query: repeated,
    constraintRect: aroundSecond
  })
  assert.ok(picked)
  assert.equal(picked.rects.length, 1)
  assert.equal(picked.rects[0].top, (VIEW.y1 - 300) * SCALE - FONT * SCALE)
  // 两处都不沾边 → 有歧义，放弃
  const nowhere = pdfRectToScreen([30, 700, 300, 720], { view: VIEW, scale: SCALE })
  assert.equal(
    computeTextLayerHighlight({
      textContent: { items: twice },
      viewport,
      query: repeated,
      constraintRect: nowhere
    }),
    null
  )
}

// 8. 首尾项按字符比例裁剪：整行一个 item 时只高亮句子所在的那一段
{
  const viewport = makeViewport(1)
  const line = 'Lead-in words here. TARGET SENTENCE HERE. trailing words'
  const hit = computeTextLayerHighlight({
    textContent: { items: [makeItem(line, 0, 700, 100)] },
    viewport,
    query: 'TARGET SENTENCE HERE.'
  })
  assert.ok(hit)
  const folded = foldTextForMatch(line)
  const start = folded.indexOf(foldTextForMatch('TARGET SENTENCE HERE.'))
  const expectedLeft = (100 * start) / folded.length
  assert.ok(Math.abs(hit.rects[0].left - expectedLeft) < 1e-9)
  assert.ok(hit.rects[0].width < 100 && hit.rects[0].width > 0)
}

// 9. 中段字形差异时退化为首尾锚定（partial=true）；旋转页/过短查询直接放弃
{
  const viewport = makeViewport(1)
  const long =
    'Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega end.'
  const corrupted = long.replace('kappa lambda', 'k@ppa l@mbda')
  const hit = computeTextLayerHighlight({
    textContent: { items: [makeItem(long, 0, 700, 300)] },
    viewport,
    query: corrupted
  })
  assert.ok(hit)
  assert.equal(hit.partial, true)
  assert.equal(
    computeTextLayerHighlight({
      textContent: { items: PAGE_ITEMS },
      viewport: makeViewport(1, 90),
      query: SENTENCE
    }),
    null
  )
  assert.equal(
    computeTextLayerHighlight({ textContent: { items: PAGE_ITEMS }, viewport, query: 'SANT' }),
    null
  )
  assert.equal(
    computeTextLayerHighlight({
      textContent: { items: PAGE_ITEMS },
      viewport,
      query: 'This sentence does not exist on the page at all.'
    }),
    null
  )
}

// 10. 阅读顺序聚行：句子从左栏底部跨到右栏顶部时，首行仍是句子起点；
//     同一基线但分属两栏的项不会被并成一行
{
  const viewport = makeViewport(1)
  const items = [
    makeItem('The structure of OsMYB73 protein was also predicted ', 40, 100, 200),
    makeItem('and the results revealed two SANT domains.', 320, 700, 200)
  ]
  const hit = computeTextLayerHighlight({
    textContent: { items },
    viewport,
    query:
      'The structure of OsMYB73 protein was also predicted and the results revealed two SANT domains.'
  })
  assert.ok(hit)
  assert.equal(hit.rects.length, 2)
  // 第一条矩形是左栏底部（起点），而不是页面更靠上的右栏续行
  assert.equal(hit.rects[0].left, 40)
  assert.ok(hit.rects[0].top > hit.rects[1].top)
  const sameBaseline = computeTextLayerHighlight({
    textContent: {
      items: [
        makeItem('left column words ', 40, 500, 100),
        makeItem('right column words', 320, 500, 100)
      ]
    },
    viewport,
    query: 'left column words right column words'
  })
  assert.equal(sameBaseline.rects.length, 2)
}

console.log('pdfHighlight.spec.js: all tests passed')
