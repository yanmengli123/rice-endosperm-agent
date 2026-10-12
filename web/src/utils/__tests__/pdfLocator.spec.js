import assert from 'node:assert/strict'
import { figureSourceSelector, tableSourceSelector } from '../figureCard.js'
import {
  evidenceSourceIds,
  evidenceWithAssetSelector,
  findSourceEvidence,
  pdfLocatorRequest
} from '../pdfLocator.js'

const source = {
  kb_id: 'kb',
  file_id: 'file',
  parse_revision_id: 'rev',
  source_sha256: 'a'.repeat(64),
  span_id: 'old-span',
  run_id: 'run'
}
const evidence = {
  source,
  locator: { anchor_id: 'old-anchor', highlight: { quote: 'parent sentence' } }
}
const figure = figureSourceSelector({
  source_selector: { ...source, figure_label: 'Figure 2', panel_label: 'A' }
})
const selected = evidenceWithAssetSelector(evidence, figure)
const request = pdfLocatorRequest(selected)
assert.deepEqual(request, {
  kb_id: 'kb',
  file_id: 'file',
  parse_revision_id: 'rev',
  source_sha256: source.source_sha256,
  figure_label: 'Figure 2',
  panel_label: 'A'
})
assert.equal(selected.source.run_id, 'run')

const table = tableSourceSelector({
  source_selector: { ...source, table_id: 'table', row_index: 0, column_index: 1 }
})
assert.deepEqual(pdfLocatorRequest(evidenceWithAssetSelector(evidence, table)), {
  kb_id: 'kb',
  file_id: 'file',
  parse_revision_id: 'rev',
  source_sha256: source.source_sha256,
  table_id: 'table',
  row_index: 0,
  column_index: 1
})
assert.equal(tableSourceSelector({ source_selector: { ...table, row_index: -1 } }), null)
assert.equal(tableSourceSelector({ source_selector: { ...table, column_index: null } }), null)
assert.equal(evidenceWithAssetSelector(evidence, { ...figure, file_id: 'other' }), null)
assert.equal(
  evidenceWithAssetSelector(evidence, { ...figure, source_sha256: 'b'.repeat(64) }),
  null
)
assert.equal(pdfLocatorRequest(evidence).span_id, 'old-span')
assert.equal(
  pdfLocatorRequest({ ...evidence, source: { ...source, span_id: null, panel_label: 'A' } })
    .anchor_id,
  'old-anchor'
)
assert.equal(
  pdfLocatorRequest({ ...evidence, source: { ...source, span_id: null, panel_label: 'A' } })
    .panel_label,
  undefined
)
const caption = {
  ...evidence,
  evidence_id: 'ev_physical',
  source: { ...source, span_evidence_id: 'evs_caption' }
}
assert.deepEqual(evidenceSourceIds(caption), ['ev_physical', 'evs_caption'])
assert.equal(findSourceEvidence([caption], 'evs_caption'), caption)
assert.equal(findSourceEvidence([caption], 'ev_physical'), caption)
assert.equal(findSourceEvidence([caption], 'evs_missing'), null)
assert.equal(
  findSourceEvidence([caption, { ...caption, evidence_id: 'ev_other' }], 'evs_caption'),
  null
)
console.log('pdfLocator.spec: passed (exclusive selectors, cell routing, immutable source lineage)')
