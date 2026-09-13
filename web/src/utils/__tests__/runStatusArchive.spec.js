import assert from 'node:assert/strict'

import {
  RUN_STATUS_ARCHIVE_MAX_ENTRIES,
  assembleEvidenceFields,
  assembleTraceProjection,
  createArchiveEntry,
  evictArchiveEntries
} from '../runStatusArchive.js'

const run = () => {
  // ---- createArchiveEntry：字段与线程槽位同名同义，默认值为空档 ----
  const entry = createArchiveEntry('run-1')
  assert.equal(entry.runId, 'run-1')
  assert.equal(entry.loading, false)
  assert.equal(entry.loaded, false)
  assert.equal(entry.trace, null)
  assert.deepEqual(entry.evidence, [])
  assert.equal(entry.traceExpired, false)
  assert.equal(entry.evidenceError, false)

  // ---- assembleTraceProjection：快照离线重建，run_id 回填、span 去重 ----
  const state = assembleTraceProjection('run-abc', {
    run_id: 'run-abc',
    spans: [
      { span_id: 's1', name: 'run.lifecycle.started', started_at: '2026-09-13T10:00:00Z' },
      { span_id: 's2', name: 'model.generate', started_at: '2026-09-13T10:00:01Z' }
    ],
    summary: { status: 'COMPLETED', total_tokens: 128 },
    projection_sequence: 5,
    snapshot_sequence: 5
  })
  assert.equal(state.runId, 'run-abc')
  assert.equal(Object.keys(state.spans).length, 2)
  assert.equal(state.summary.status, 'COMPLETED')
  assert.ok(state.spans.s1.started_at instanceof Date)
  assert.ok(state.lastAppliedSequence >= 5)

  // 快照缺失 run_id 时以入参 runId 兜底（服务端始终带，但组装层不依赖）
  const fallbackState = assembleTraceProjection('run-xyz', { spans: [], summary: null })
  assert.equal(fallbackState.runId, 'run-xyz')
  assert.equal(fallbackState.summary, null)
  assert.deepEqual(fallbackState.spans, {})

  // 空快照（轨迹被清理后返回体为空）不抛错，产出空投影
  const emptyState = assembleTraceProjection('run-err', null)
  assert.equal(emptyState.runId, 'run-err')
  assert.deepEqual(emptyState.spans, {})

  // ---- assembleEvidenceFields：归一化 / 越界形态防御 ----
  const fields = assembleEvidenceFields({
    evidence: [{ evidence_id: 'e1' }],
    summary: { total: 1 },
    retrievals: [],
    issues: null,
    evidence_role: 'PRIMARY',
    claim_binding_status: null,
    projection_status: 'OK',
    source_manifest: { document_evidence_requested: true }
  })
  assert.deepEqual(fields.evidence, [{ evidence_id: 'e1' }])
  assert.equal(fields.evidenceSummary.total, 1)
  assert.deepEqual(fields.evidenceRetrievals, [])
  assert.deepEqual(fields.evidenceIssues, [])
  assert.equal(fields.evidenceRole, 'PRIMARY')
  assert.equal(fields.sourceManifest.document_evidence_requested, true)

  // 响应体异常（undefined / 字符串）时全部归空，不抛错
  const blankFields = assembleEvidenceFields(undefined)
  assert.deepEqual(blankFields.evidence, [])
  assert.equal(blankFields.evidenceSummary, null)
  assert.equal(blankFields.sourceManifest, null)
  const stringFields = assembleEvidenceFields('error text')
  assert.deepEqual(stringFields.evidence, [])

  // ---- evictArchiveEntries：LRU 驱逐、焦点保护、map 顺序即新鲜度 ----
  const entries = new Map()
  for (let i = 1; i <= RUN_STATUS_ARCHIVE_MAX_ENTRIES; i += 1) entries.set(`run-${i}`, i)
  assert.deepEqual(evictArchiveEntries(entries, RUN_STATUS_ARCHIVE_MAX_ENTRIES, null), [])

  entries.set('run-new', 'new')
  const evicted = evictArchiveEntries(entries, RUN_STATUS_ARCHIVE_MAX_ENTRIES, null)
  assert.deepEqual(evicted, ['run-1'])
  assert.equal(entries.size, RUN_STATUS_ARCHIVE_MAX_ENTRIES)
  assert.ok(!entries.has('run-1'))
  assert.ok(entries.has('run-new'))

  // 焦点保护：protectedKey 对应条目永不驱逐，驱逐顺延到次旧条目
  const focusEntries = new Map([
    ['a', 1],
    ['b', 2],
    ['c', 3]
  ])
  assert.deepEqual(evictArchiveEntries(focusEntries, 2, 'a'), ['b'])
  assert.ok(focusEntries.has('a'))
  assert.ok(focusEntries.has('c'))

  // 焦点之外的全部驱逐后停止（保护键保留，不会无限循环）
  const allProtected = new Map([
    ['a', 1],
    ['b', 2]
  ])
  assert.deepEqual(evictArchiveEntries(allProtected, 1, 'a'), ['b'])
  assert.equal(allProtected.size, 1)
  assert.ok(allProtected.has('a'))

  // 非法上限不驱逐
  const noLimit = new Map([
    ['a', 1],
    ['b', 2]
  ])
  assert.deepEqual(evictArchiveEntries(noLimit, 0, null), [])

  console.log('runStatusArchive.spec: all assertions passed')
}

run()
