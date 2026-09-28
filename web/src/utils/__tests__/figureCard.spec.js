import assert from 'node:assert/strict'

import {
  extractCitationReadyFromHistory,
  figureAssetUri,
  figureCardTitle,
  figureRefClickAction,
  figureRefsForMessage,
  inlineFiguresForMessage,
  normalizeFigureRefs,
  normalizeVerifiedFigures,
  normalizeVerifiedTables,
  tablesForMessage
} from '../figureCard.js'

const figure = {
  binding_id: 'vlb_x',
  kb_id: 'kb-a',
  file_id: 'file-a',
  revision_id: 'pr_1',
  asset_name: '0123456789abcdef012345678-fig1.png',
  asset_sha256: 'b'.repeat(64),
  media_type: 'image/png',
  page: 4,
  evidence_id: 'ev_1',
  figure_label: 'Figure 1',
  caption: 'Figure 1. Expression patterns of OsMYB73.'
}

const run = () => {
  // URI 组装：合法三段式；缺段/非法字符 → null，不抛
  assert.equal(figureAssetUri(figure), 'kbasset://file-a/pr_1/0123456789abcdef012345678-fig1.png')
  assert.equal(figureAssetUri({ ...figure, asset_name: '' }), null)
  assert.equal(figureAssetUri({ ...figure, asset_name: 'fig 1.png' }), null)
  assert.equal(figureAssetUri({ ...figure, file_id: 'a/b' }), null)
  assert.equal(figureAssetUri(null), null)

  // 标题回退链：caption → figure_label → 中性文案
  assert.equal(figureCardTitle(figure), 'Figure 1. Expression patterns of OsMYB73.')
  assert.equal(figureCardTitle({ ...figure, caption: '' }), 'Figure 1')
  assert.equal(figureCardTitle({ ...figure, caption: '', figure_label: '' }), '图 · 第4页')
  assert.equal(figureCardTitle({ caption: '', figure_label: '', page: null }), '图')

  // 归一化：非数组 → []；缺 kb_id / 非法 URI 的条目被剔除
  assert.deepEqual(normalizeVerifiedFigures(undefined), [])
  assert.deepEqual(
    normalizeVerifiedFigures([
      figure,
      { ...figure, kb_id: '' },
      { ...figure, asset_name: '' },
      null
    ]),
    [figure]
  )

  // 历史恢复：只读最后一条 AI 消息的 citation_ready；缺失即 null（不回退旧轮、不读 citation_binding）
  const citation = {
    status: 'VERIFIED',
    page: 4,
    kb_id: 'kb-a',
    revision_id: 'pr_1',
    filename: 'a.pdf'
  }
  const history = [
    { type: 'human', content: 'Figure 1 在哪' },
    { type: 'ai', extra_metadata: { citation_ready: { citation, figures: [figure] } } },
    { type: 'tool', content: '...' }
  ]
  assert.deepEqual(extractCitationReadyFromHistory(history), {
    citation,
    figures: [figure],
    figureRefs: [],
    tables: []
  })

  const darkLaunch = [
    { type: 'human', content: 'Figure 1 在哪' },
    {
      type: 'ai',
      extra_metadata: {
        citation_binding: { figure_projection: { status: 'attached', figures: [figure] } },
        citation_ready: { citation }
      }
    }
  ]
  assert.deepEqual(extractCitationReadyFromHistory(darkLaunch), {
    citation,
    figures: [],
    figureRefs: [],
    tables: []
  })

  const noPayload = [
    { type: 'ai', extra_metadata: { citation_ready: { citation, figures: [figure] } } },
    { type: 'human', content: '再问一个' },
    { role: 'assistant', extra_metadata: {} }
  ]
  assert.equal(extractCitationReadyFromHistory(noPayload), null)
  assert.equal(extractCitationReadyFromHistory([]), null)
  assert.equal(extractCitationReadyFromHistory(undefined), null)

  // 答案气泡内图卡：只挂该轮最后一条 AI 消息；已落库载荷优先；否则按 run_id 取本会话暂存
  const human = { type: 'human', content: 'Figure 1 在哪' }
  const toolAi = { type: 'ai', id: 'ai-tool', run_id: 'run-1', tool_calls: [{ name: 'search' }] }
  const finalAi = {
    type: 'ai',
    id: 'ai-final',
    run_id: 'run-1',
    extra_metadata: { run_id: 'run-1' }
  }
  const conv = { messages: [human, toolAi, finalAi], status: 'finished' }
  const figuresByRun = { 'run-1': [figure] }
  assert.deepEqual(inlineFiguresForMessage(finalAi, conv, figuresByRun), [figure])
  assert.deepEqual(inlineFiguresForMessage(toolAi, conv, figuresByRun), []) // 中间消息不挂
  assert.deepEqual(inlineFiguresForMessage(human, conv, figuresByRun), [])
  assert.deepEqual(inlineFiguresForMessage(finalAi, conv, {}), []) // 无暂存、无落库 → 空
  const persistedAi = {
    type: 'ai',
    id: 'ai-p',
    run_id: 'run-2',
    extra_metadata: {
      citation_ready: { citation: {}, figures: [{ ...figure, caption: 'persisted' }] }
    }
  }
  const conv2 = { messages: [human, persistedAi] }
  // 已落库优先于暂存
  assert.equal(
    inlineFiguresForMessage(persistedAi, conv2, { 'run-2': [figure] })[0].caption,
    'persisted'
  )
  // 同 id 的副本对象也能命中（displayItem 可能是拷贝）
  assert.deepEqual(inlineFiguresForMessage({ ...finalAi }, conv, figuresByRun), [figure])

  // ---- 图表锚点（ADR-0008 figure_refs）----
  const refFigure = {
    ref: 'F1',
    kind: 'figure',
    label: 'Figure 2',
    figure_index: 0,
    evidence_id: 'ev_1'
  }
  const refTable = {
    ref: 'F2',
    kind: 'table',
    label: 'Table 1',
    figure_index: null,
    evidence_id: 'ev_9'
  }

  // 归一化：ref 形态（F\d{1,3}）+ label 必填
  assert.deepEqual(normalizeFigureRefs(undefined), [])
  assert.deepEqual(
    normalizeFigureRefs([refFigure, refTable, { ref: 'X1', label: 'bad' }, { ref: 'F3' }, null]),
    [refFigure, refTable]
  )
  assert.deepEqual(
    normalizeFigureRefs([{ ...refFigure, visual_status: 'FUTURE_UNKNOWN' }])[0].visual_status,
    'REFERENCE_ONLY'
  )
  assert.equal(
    normalizeFigureRefs([{ ...refFigure, visual_status: 'VERIFIED_CAPTION_ONLY' }])[0]
      .visual_status,
    'VERIFIED_CAPTION_ONLY'
  )

  // 点击裁决：有卡片 → scroll；无卡片有证据（Table/suppressed）→ open-source；其余 none
  assert.equal(figureRefClickAction('F1', [refFigure, refTable], [figure]).type, 'scroll')
  assert.deepEqual(figureRefClickAction('F1', [refFigure], [figure]).figure, figure)
  assert.equal(figureRefClickAction('F2', [refFigure, refTable], [figure]).type, 'open-source')
  assert.equal(figureRefClickAction('F2', [refFigure, refTable], [figure]).evidenceId, 'ev_9')
  assert.equal(figureRefClickAction('F1', [], [figure]).type, 'none')
  assert.equal(figureRefClickAction('F9', [refFigure], [figure]).type, 'none')
  // 下标越界 → 回落到证据通道（open-source），比无动作更有用
  assert.equal(
    figureRefClickAction('F1', [{ ...refFigure, figure_index: 7 }], [figure]).type,
    'open-source'
  )
  // 下标越界且无证据 → none
  assert.equal(
    figureRefClickAction('F1', [{ ...refFigure, figure_index: 7, evidence_id: '' }], [figure]).type,
    'none'
  )

  // 消息级锚点数据源：与图卡同语义（只挂最后一条 AI；落库优先；run 暂存桥接）
  assert.deepEqual(figureRefsForMessage(finalAi, conv, { 'run-1': [refFigure, refTable] }), [
    refFigure,
    refTable
  ])
  assert.deepEqual(figureRefsForMessage(toolAi, conv, { 'run-1': [refFigure] }), [])
  assert.deepEqual(figureRefsForMessage(finalAi, conv, {}), [])
  const persistedRefsAi = {
    type: 'ai',
    id: 'ai-pr',
    run_id: 'run-3',
    extra_metadata: {
      citation_ready: {
        citation: {},
        figures: [],
        figure_refs: [{ ...refFigure, label: 'persisted' }]
      }
    }
  }
  assert.equal(
    figureRefsForMessage(persistedRefsAi, { messages: [human, persistedRefsAi] }, {})[0].label,
    'persisted'
  )

  // 历史恢复携带 figure_refs（加法；缺席 → []）
  const historyWithRefs = [
    { type: 'human', content: '见图' },
    {
      type: 'ai',
      extra_metadata: {
        citation_ready: { citation, figures: [figure], figure_refs: [refFigure, refTable] }
      }
    }
  ]
  assert.deepEqual(extractCitationReadyFromHistory(historyWithRefs).figureRefs, [
    refFigure,
    refTable
  ])

  // ---- 表格卡片（ADR-0008 P2）----
  const tableRow = [{ text: 'WT', rowspan: 1, colspan: 1, header: false }]
  const tableCard = {
    table_id: 'tbl_abc',
    kb_id: 'kb-a',
    revision_id: 'pr_1',
    rows: [tableRow],
    row_count: 1,
    col_count: 1,
    evidence_id: 'ev_9'
  }

  // 归一化：table_id + kb_id + rows 数组必填
  assert.deepEqual(normalizeVerifiedTables(undefined), [])
  assert.deepEqual(normalizeVerifiedTables([tableCard, { table_id: 'x' }, null]), [tableCard])

  // 点击裁决：table ref + table_index → scroll-table；figure ref 不误命中表格域
  const refTableOk = {
    ref: 'F2',
    kind: 'table',
    label: 'Table 1',
    table_index: 0,
    evidence_id: 'ev_9'
  }
  const refTableNo = {
    ref: 'F2',
    kind: 'table',
    label: 'Table 1',
    table_index: null,
    evidence_id: 'ev_9'
  }
  assert.equal(figureRefClickAction('F2', [refTableOk], [figure], [tableCard]).type, 'scroll-table')
  assert.equal(figureRefClickAction('F2', [refTableOk], [figure], []).type, 'open-source')
  assert.equal(figureRefClickAction('F2', [refTableNo], [figure], [tableCard]).type, 'open-source')
  // figure ref 的 figure_index 不读 tables 域
  const refFig = {
    ref: 'F1',
    kind: 'figure',
    label: 'Figure 2',
    figure_index: null,
    evidence_id: 'ev_1'
  }
  assert.equal(figureRefClickAction('F1', [refFig], [], [tableCard]).type, 'open-source')

  // 消息级数据源：落库优先 → run 暂存桥接；只挂最后一条 AI
  assert.deepEqual(tablesForMessage(finalAi, conv, { 'run-1': [tableCard] }), [tableCard])
  assert.deepEqual(tablesForMessage(toolAi, conv, { 'run-1': [tableCard] }), [])
  assert.deepEqual(tablesForMessage(finalAi, conv, {}), [])

  console.log('figureCard: all assertions passed')
}

run()
