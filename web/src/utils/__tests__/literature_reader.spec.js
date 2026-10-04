import assert from 'node:assert/strict'
import {
  normalizeReaderPreferences,
  readReaderPreferences,
  writeReaderPreferences,
  parseLiteratureLink,
  normalizeReaderAnchor,
  locateParallelAnchor,
  READER_TABS,
  moveReaderTab
} from '../literature_reader.js'
assert.deepEqual(
  normalizeReaderPreferences({ tab: 'javascript:bad', fontSize: -4, pdfScale: Infinity }),
  {
    tab: 'original',
    fontSize: 16,
    pdfScale: 1,
    pdfFitWidth: true,
    autoHideMissing: true,
    tabOrder: READER_TABS,
    hiddenTabs: []
  }
)
const ordered = normalizeReaderPreferences({
  tabOrder: ['pdf', 'pdf', 'bad'],
  hiddenTabs: ['original', 'pdf', 'pdf', 'bad']
})
assert.deepEqual(ordered.tabOrder, ['pdf', ...READER_TABS.filter((key) => key !== 'pdf')])
assert.deepEqual(ordered.hiddenTabs, ['pdf'])
assert.deepEqual(moveReaderTab(READER_TABS, 'pdf', 'original').slice(0, 2), ['pdf', 'original'])
assert.deepEqual(moveReaderTab(READER_TABS, 'bad', 'original'), READER_TABS)
const memory = new Map(),
  storage = { getItem: (key) => memory.get(key), setItem: (key, value) => memory.set(key, value) }
writeReaderPreferences(storage, 'alice', {
  tab: 'noteL2',
  fontSize: 20,
  pdfScale: 8,
  pdfFitWidth: false
})
assert.equal(readReaderPreferences(storage, 'alice').pdfScale, 4)
assert.equal(readReaderPreferences(storage, 'bob').tab, 'original')
assert.equal(
  readReaderPreferences(
    {
      getItem: () => {
        throw Error('denied')
      }
    },
    'alice'
  ).fontSize,
  16
)
assert.equal(parseLiteratureLink('doi-10.1234_example_L1_note.md#摘要'), 'doi-10.1234_example')
assert.equal(parseLiteratureLink('10.1234/example'), '10.1234/example')
for (const invalid of [
  'https://evil/x.md',
  'javascript:bad',
  '../escape.md',
  'C:\\secret.md',
  '/private.md'
])
  assert.equal(parseLiteratureLink(invalid), '')
console.log('Reader preferences and link boundary tests passed')
assert.equal(normalizeReaderAnchor('#%E6%A0%B8%E5%BF%83%20%E6%A6%82%E5%BF%B5'), '核心-概念')
assert.equal(normalizeReaderAnchor('Results & Discussion'), 'results-discussion')
assert.equal(normalizeReaderAnchor('#^p17'), '^p17')
assert.equal(normalizeReaderAnchor('%broken'), 'broken')
assert.deepEqual(
  locateParallelAnchor([{ original: '## Results' }, { original: '## Results' }], 'results-1'),
  { index: 1, anchor: 'results' }
)
assert.equal(
  locateParallelAnchor([{ original: 'Block ^p1' }, { original: 'Duplicate ^p1' }], '^p1'),
  null
)
assert.equal(locateParallelAnchor([{ original: '## Results' }], 'missing'), null)
