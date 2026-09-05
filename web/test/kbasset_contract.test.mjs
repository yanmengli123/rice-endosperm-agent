import assert from 'node:assert/strict'
import test from 'node:test'

import { buildKnowledgeAssetUrl, parseKbAssetUri } from '../src/utils/kbasset_contract.js'

test('kbasset URI maps to the authenticated document asset endpoint', () => {
  const parsed = parseKbAssetUri('kbasset://file_1/spr_2/figure-3.jpg')

  assert.deepEqual(parsed, {
    fileId: 'file_1',
    revisionId: 'spr_2',
    assetName: 'figure-3.jpg'
  })
  assert.equal(
    buildKnowledgeAssetUrl('kb_9', parsed),
    '/api/knowledge/databases/kb_9/documents/file_1/revisions/spr_2/assets/figure-3.jpg'
  )
})

test('kbasset contract rejects storage paths and forged route identities', () => {
  assert.equal(parseKbAssetUri('kbasset://file_1/spr_2/folder/figure.jpg'), null)
  assert.equal(parseKbAssetUri('http://localhost:9000/knowledgebases/private.jpg'), null)
  assert.throws(
    () =>
      buildKnowledgeAssetUrl('kb_9', {
        fileId: 'file_1',
        revisionId: '../spr_2',
        assetName: 'figure.jpg'
      }),
    /revisionId is invalid/
  )
})
