import assert from 'node:assert/strict'
import {
  normalizeSourceQuotes,
  sourceQuotesForMessage,
  extractCitationReadyFromHistory
} from '../figureCard.js'

const quote = {
  evidence_id: 'ev_1',
  quote: '  原文（Figure 2A–2C）与 [Tables 1 and 2]。\n',
  quote_hash: 'a'.repeat(64),
  page_number: 5,
  source: {
    kb_id: 'kb',
    file_id: 'file',
    parse_revision_id: 'rev',
    anchor_id: 'anchor',
    source_sha256: 'b'.repeat(64)
  },
  locator: { precision: 'REGION', fragments: [{ page_index: 4, bbox: [10, 10, 100, 100] }] },
  targets: [{ label: 'Table 1', status: 'MISSING' }]
}
assert.deepEqual(normalizeSourceQuotes([quote, null, { ...quote, quote_hash: 'invalid' }]), [quote])
assert.equal(normalizeSourceQuotes([quote])[0].quote, quote.quote)
assert.deepEqual(
  normalizeSourceQuotes([{ ...quote, source: { ...quote.source, anchor_id: '' } }]),
  []
)
const message = { id: 'ai', type: 'ai', run_id: 'run' }
const conv = { messages: [{ id: 'tool-ai', type: 'ai' }, message] }
assert.deepEqual(sourceQuotesForMessage(message, conv, { run: [quote] }), [quote])
assert.deepEqual(sourceQuotesForMessage(conv.messages[0], conv, { run: [quote] }), [])
const persisted = { ...message, extra_metadata: { citation_ready: { source_quotes: [quote] } } }
assert.deepEqual(sourceQuotesForMessage(persisted, conv, {}), [quote])
assert.deepEqual(
  sourceQuotesForMessage({ ...message, extra_metadata: { citation_ready: {} } }, conv, {
    run: [quote]
  }),
  []
)
assert.deepEqual(extractCitationReadyFromHistory([persisted]).sourceQuotes, [quote])
console.log('sourceQuotes: exact text, validation, stream/history publication passed')
