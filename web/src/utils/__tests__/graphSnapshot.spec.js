import assert from 'node:assert/strict'

import {
  exportGraphSnapshotUrl,
  extractGraphSnapshotFromHistory,
  graphCanvasData,
  graphExportSourceForConversation,
  graphWorkbenchRoute,
  groupEdgesByRelationGroup,
  inlineGraphForMessage,
  normalizeGraphSnapshot,
  pendingEdgeCount,
  suppressedCandidateCount
} from '../graphSnapshot.js'

const snapshot = {
  schema: 'graph_snapshot_v1',
  retrieval_id: 'kr-1',
  outcome: 'HIT',
  nodes: [
    { entity_id: 'e1', kb_id: 'kb', name: 'GS3', label: 'Gene' },
    { entity_id: 'e2', kb_id: 'kb', name: 'grain size', label: 'Trait' }
  ],
  edges: [
    {
      triple_id: 't1',
      kb_id: 'kb',
      source_entity_id: 'e1',
      target_entity_id: 'e2',
      predicate: 'regulates',
      review_status: 'APPROVED'
    }
  ]
}

const run = () => {
  assert.equal(normalizeGraphSnapshot(null), null)
  assert.equal(normalizeGraphSnapshot({ schema: 'unknown' }), null)
  assert.deepEqual(normalizeGraphSnapshot(snapshot), snapshot)

  const dangling = normalizeGraphSnapshot({
    ...snapshot,
    edges: [
      ...snapshot.edges,
      { ...snapshot.edges[0], triple_id: 'bad', target_entity_id: 'missing' }
    ]
  })
  assert.deepEqual(dangling.edges, snapshot.edges)

  const canvas = graphCanvasData(snapshot)
  assert.equal(canvas.nodes[0].id, 'e1')
  assert.equal(canvas.edges[0].id, 't1')
  assert.equal(canvas.edges[0].source_id, 'e1')

  const human = { type: 'human', id: 'h1' }
  const final = { type: 'ai', id: 'a1', run_id: 'run-1', extra_metadata: {} }
  const conv = { messages: [human, final] }
  assert.deepEqual(inlineGraphForMessage(final, conv, { 'run-1': snapshot }), snapshot)
  assert.equal(inlineGraphForMessage(human, conv, { 'run-1': snapshot }), null)

  const persisted = {
    ...final,
    extra_metadata: { graph_snapshot: { ...snapshot, retrieval_id: 'persisted' } }
  }
  assert.equal(
    inlineGraphForMessage(persisted, { messages: [human, persisted] }, { 'run-1': snapshot })
      .retrieval_id,
    'persisted'
  )
  assert.deepEqual(
    extractGraphSnapshotFromHistory([human, persisted]),
    persisted.extra_metadata.graph_snapshot
  )

  const mixed = {
    ...snapshot,
    edges: [
      ...snapshot.edges,
      { ...snapshot.edges[0], triple_id: 't2', review_status: 'CANDIDATE' }
    ],
    suppressed: { review_policy: 3 }
  }
  assert.equal(pendingEdgeCount(mixed), 1)
  assert.equal(
    pendingEdgeCount({
      ...mixed,
      edges: [{ ...mixed.edges[0], candidate_parallel_count: 4, review_status: 'APPROVED' }]
    }),
    4
  )
  assert.equal(pendingEdgeCount(snapshot), 0)
  assert.equal(suppressedCandidateCount(mixed), 3)
  assert.equal(suppressedCandidateCount(snapshot), 0)
  assert.equal('review_status' in canvas.edges[0].properties, true)

  const sliced = graphCanvasData(mixed, 1)
  assert.equal(sliced.edges.length, 1)
  assert.equal(sliced.nodes.length, 2)
  const grouped = groupEdgesByRelationGroup(mixed)
  assert.equal(grouped.length, 1)
  assert.equal(grouped[0].edges.length, 2)

  const withRun = { ...final, run_id: 'run-9', extra_metadata: { graph_snapshot: snapshot } }
  assert.deepEqual(graphExportSourceForConversation({ messages: [human, withRun] }), {
    snapshot,
    runId: 'run-9'
  })
  assert.equal(graphExportSourceForConversation({ messages: [human, final] }), null)
  assert.equal(
    exportGraphSnapshotUrl('run-9', 'csv'),
    '/api/agent/runs/run-9/graph-snapshot-export?format=csv'
  )
  assert.equal(exportGraphSnapshotUrl('r/1', 'json').includes(encodeURIComponent('r/1')), true)

  const withKb = {
    ...snapshot,
    seed_display_name: 'GS3',
    nodes: snapshot.nodes.map((node, i) => ({ ...node, kb_id: i === 0 ? 'kb-main' : node.kb_id }))
  }
  assert.deepEqual(graphWorkbenchRoute(withKb), {
    path: '/extensions/knowledgebase/kb-main',
    query: { tab: 'graph', ws: 'explorer', seed: 'GS3' }
  })
  assert.equal(graphWorkbenchRoute(snapshot).query.seed, undefined)
  assert.equal(graphWorkbenchRoute(null), null)

  console.log('graphSnapshot: all assertions passed')
}

run()
