import assert from 'node:assert/strict'

import {
  buildFollowUpQuote,
  buildSelectionPreview,
  computePopupPosition
} from '../selectionPopup.js'

const rect = (left, top, right, bottom) => ({
  left,
  top,
  right,
  bottom,
  width: right - left,
  height: bottom - top
})

const run = () => {
  // computePopupPosition：无有效矩形时返回 null
  assert.equal(
    computePopupPosition([], { width: 800, height: 600 }, { width: 120, height: 40 }),
    null
  )

  // 默认放在选区首行上方、水平居中
  assert.deepEqual(
    computePopupPosition(
      [rect(300, 200, 500, 220)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ),
    { left: 340, top: 152 }
  )

  // 上方空间不足时翻转到末行下方
  assert.deepEqual(
    computePopupPosition(
      [rect(300, 10, 500, 30)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ),
    { left: 340, top: 38 }
  )

  // 多行选区翻转时以末行底部为锚
  assert.deepEqual(
    computePopupPosition(
      [rect(300, 10, 500, 30), rect(100, 30, 500, 50)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ),
    { left: 340, top: 58 }
  )

  // 选区整体在视口上方（如程序化选区）时钳制回视口内
  assert.deepEqual(
    computePopupPosition(
      [rect(300, -300, 500, -280)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ),
    { left: 340, top: 8 }
  )

  // 选区整体在视口下方时钳制回视口内
  assert.deepEqual(
    computePopupPosition(
      [rect(300, 700, 500, 720)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ),
    { left: 340, top: 600 - 40 - 8 }
  )

  // 选区贴近视口左缘时向内收缩
  assert.equal(
    computePopupPosition(
      [rect(10, 200, 60, 220)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ).left,
    8
  )

  // 选区贴近视口右缘时向内收缩
  assert.equal(
    computePopupPosition(
      [rect(740, 200, 790, 220)],
      { width: 800, height: 600 },
      { width: 120, height: 40 }
    ).left,
    800 - 120 - 8
  )

  // buildFollowUpQuote：单行加引用前缀
  assert.equal(buildFollowUpQuote('水稻胚乳'), '> 水稻胚乳')

  // 多行逐行加前缀，空行保留为 “>”
  assert.equal(buildFollowUpQuote('第一行\n\n第二行'), '> 第一行\n>\n> 第二行')

  // 3 个以上连续换行折叠为空行
  assert.equal(buildFollowUpQuote('第一行\n\n\n\n第二行'), '> 第一行\n>\n> 第二行')

  // CRLF 归一
  assert.equal(buildFollowUpQuote('第一行\r\n第二行'), '> 第一行\n> 第二行')

  // 超长截断为 2000 字并加省略号
  const quoted = buildFollowUpQuote('a'.repeat(2001))
  assert.equal(quoted.length, '> '.length + 2000 + 1)
  assert.ok(quoted.endsWith('…'))

  // 纯空白文本返回空串
  assert.equal(buildFollowUpQuote('   \n  '), '')

  // buildSelectionPreview：空白折叠为单空格
  assert.equal(buildSelectionPreview('水稻  胚乳\n淀粉合成'), '水稻 胚乳 淀粉合成')

  // 超过 60 字截断加省略号
  assert.equal(buildSelectionPreview('a'.repeat(61)), `${'a'.repeat(60)}…`)

  assert.equal(buildSelectionPreview(''), '')

  console.log('selectionPopup: all assertions passed')
}

run()
