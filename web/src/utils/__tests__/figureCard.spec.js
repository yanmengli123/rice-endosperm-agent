import assert from 'node:assert/strict'

import {
  extractCitationReadyFromHistory,
  figureAssetUri,
  figureCardTitle,
  normalizeVerifiedFigures
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
  assert.deepEqual(extractCitationReadyFromHistory(history), { citation, figures: [figure] })

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
  assert.deepEqual(extractCitationReadyFromHistory(darkLaunch), { citation, figures: [] })

  const noPayload = [
    { type: 'ai', extra_metadata: { citation_ready: { citation, figures: [figure] } } },
    { type: 'human', content: '再问一个' },
    { role: 'assistant', extra_metadata: {} }
  ]
  assert.equal(extractCitationReadyFromHistory(noPayload), null)
  assert.equal(extractCitationReadyFromHistory([]), null)
  assert.equal(extractCitationReadyFromHistory(undefined), null)

  console.log('figureCard: all assertions passed')
}

run()
