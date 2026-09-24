/**
 * 知识图谱治理纯逻辑测试（裸 node 运行，与 messageProcessor.spec.js 同模式）：
 *   node src/utils/graph/__tests__/reviewMeta.spec.js
 */
import assert from 'node:assert/strict'

import {
  REVIEW_STATUS_META,
  TRUST_META,
  REASON_CODES,
  BATCH_ADMISSION_META,
  EDGE_STATUS_LEGEND,
  composeReason,
  reviewStatusLabel,
  auditActionLabel,
  summarizeBatchPreview,
  edgeReviewStatus,
  edgeStyleByReviewStatus,
  shouldShowGraphConfigEmpty,
  normalizeBuildStatus,
  buildStatusSummary,
  buildTaskLabel
} from '../reviewMeta.js'

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`✓ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${name}\n  ${error?.stack || error}`)
  }
}

// ── 术语分词：人工决策与机器校验不得共用「已验证」一词 ──
test('人工决策文案不含「已验证」；机器校验文案带「机器校验」前缀', () => {
  const humanLabels = Object.values(REVIEW_STATUS_META).map((meta) => meta.label)
  for (const label of humanLabels) {
    assert.ok(!label.includes('已验证'), `人工决策标签不得用「已验证」: ${label}`)
    assert.ok(!label.includes('机器'), `人工决策标签不得混入机器语义: ${label}`)
  }
  const machineLabels = Object.values(TRUST_META).map((meta) => meta.label)
  for (const label of machineLabels) {
    if (label.startsWith('机器校验')) continue
    assert.equal(label, '未机器验证')
  }
  assert.equal(reviewStatusLabel('APPROVED'), '人工批准')
})

test('composeReason 与后端同格式，非法代码被忽略', () => {
  assert.equal(composeReason('direction_error', '方向反了'), '[DIRECTION_ERROR] 方向反了')
  assert.equal(composeReason('NOT_A_CODE', '说明'), '说明')
  assert.equal(composeReason('', '说明'), '说明')
  assert.equal(composeReason('OTHER', ''), '')
  const values = REASON_CODES.map((item) => item.value)
  assert.deepEqual(values.slice().sort(), [...new Set(values)].sort(), '原因代码闭集无重复')
})

test('批量预检汇总：准入/阻断分组与 5 条抽样', () => {
  const preview = {
    items: [
      { target_id: 't1', admissible: true, preview_quote: 'q1', pinned_chunk_id: 'c1' },
      { target_id: 't2', admissible: false, reason: 'no_ok_quote' },
      { target_id: 't3', admissible: true, preview_quote: 'q3', pinned_chunk_id: 'c3' },
      { target_id: 't4', admissible: true, preview_quote: 'q4', pinned_chunk_id: 'c4' },
      { target_id: 't5', admissible: true, preview_quote: 'q5', pinned_chunk_id: 'c5' },
      { target_id: 't6', admissible: true, preview_quote: 'q6', pinned_chunk_id: 'c6' },
      { target_id: 't7', admissible: true, preview_quote: 'q7', pinned_chunk_id: 'c7' }
    ]
  }
  const summary = summarizeBatchPreview(preview)
  assert.equal(summary.admissible.length, 6)
  assert.equal(summary.blocked.length, 1)
  assert.equal(summary.sample.length, 5)
  assert.equal(summary.sample[0].id, 't1')
  assert.ok(summarizeBatchPreview({}).sample.length === 0)
})

test('审计动作标签覆盖治理动作', () => {
  assert.equal(auditActionLabel('GOVERNANCE_SETTINGS_UPDATE'), '治理设置变更')
  assert.equal(auditActionLabel('APPROVE'), '人工批准')
  assert.equal(auditActionLabel('UNKNOWN_ACTION'), 'UNKNOWN_ACTION')
})

// ── 画布线型编码：状态 + 线型双通道（不依赖颜色）──
test('边审核状态：托管导入标记优先，缺属性返回 null', () => {
  assert.equal(edgeReviewStatus({ properties: { managed_projection: true } }), 'CANONICAL')
  assert.equal(edgeReviewStatus({ properties: { review_status: 'APPROVED' } }), 'APPROVED')
  assert.equal(edgeReviewStatus({ properties: {} }), null)
})

test('线型编码：候选虚线 / 批准实线加粗 / 规范长划线 / 冲突点线', () => {
  const candidate = edgeStyleByReviewStatus({ properties: { review_status: 'CANDIDATE' } })
  assert.deepEqual(candidate.lineDash, [6, 4])
  const approved = edgeStyleByReviewStatus({ properties: { review_status: 'APPROVED' } })
  assert.equal(approved.lineDash, undefined)
  assert.ok(approved.lineWidth > candidate.lineWidth)
  const canonical = edgeStyleByReviewStatus({ properties: { managed_projection: true } })
  assert.deepEqual(canonical.lineDash, [12, 3])
  const conflict = edgeStyleByReviewStatus({
    properties: { review_status: 'APPROVED', conflict_status: 'CONTESTED' }
  })
  assert.deepEqual(conflict.lineDash, [2, 3])
  const unknown = edgeStyleByReviewStatus({ properties: {} })
  assert.deepEqual(unknown.lineDash, [6, 4], '缺状态属性按候选虚线处理（fail-closed 显示语义）')
})

test('准入档位与图例均为冻结结构', () => {
  assert.deepEqual(Object.keys(BATCH_ADMISSION_META).sort(), ['relaxed', 'standard', 'strict'])
  assert.ok(EDGE_STATUS_LEGEND.length >= 4)
  assert.ok(EDGE_STATUS_LEGEND.every((item) => item.label && item.pattern))
})

test('新库空态不能卸载抽取器配置工作区', () => {
  const unconfigured = { build: { configured: false } }
  assert.equal(
    shouldShowGraphConfigEmpty({
      mode: 'workbench',
      activeWorkspace: 'review',
      isManagedGraph: false,
      summary: unconfigured
    }),
    true
  )
  assert.equal(
    shouldShowGraphConfigEmpty({
      mode: 'workbench',
      activeWorkspace: 'build',
      isManagedGraph: false,
      summary: unconfigured
    }),
    false,
    '构建工作区必须可达，否则空态 CTA 会形成状态死锁'
  )
  assert.equal(
    shouldShowGraphConfigEmpty({
      mode: 'workbench',
      activeWorkspace: 'review',
      isManagedGraph: true,
      summary: unconfigured
    }),
    false
  )
  assert.equal(
    shouldShowGraphConfigEmpty({
      mode: 'workbench',
      activeWorkspace: 'review',
      isManagedGraph: false,
      summary: null
    }),
    false
  )
  assert.equal(
    shouldShowGraphConfigEmpty({
      mode: 'workbench',
      activeWorkspace: 'review',
      isManagedGraph: false,
      summary: { build: { configured: true } }
    }),
    false
  )
})

test('构建状态规范化：空值与负值安全归零', () => {
  const m = normalizeBuildStatus(null)
  assert.equal(m.total, 0)
  assert.equal(m.indexed, 0)
  assert.equal(m.pending, 0)
  assert.equal(m.percent, 0)
  assert.equal(m.taskStatus, null)
  assert.equal(m.active, false)
  assert.equal(m.configured, false)
  const weird = normalizeBuildStatus({
    total_chunks: 'abc',
    indexed_chunks: -5,
    build_task_progress: 320
  })
  assert.equal(weird.total, 0)
  assert.equal(weird.indexed, 0)
  assert.equal(weird.taskProgress, 100)
})

test('构建状态规范化：计数/百分比/活跃态', () => {
  const m = normalizeBuildStatus({
    total_chunks: 100,
    indexed_chunks: 40,
    pending_chunks: 60,
    dead_chunks: 2,
    build_task_status: 'running',
    build_task_progress: 55.6,
    configured: true
  })
  assert.equal(m.percent, 40)
  assert.equal(m.active, true)
  assert.equal(m.taskProgress, 55)
  assert.equal(m.configured, true)
  const done = normalizeBuildStatus({
    total_chunks: 10,
    indexed_chunks: 10,
    pending_chunks: 0,
    build_task_status: 'success'
  })
  assert.equal(done.percent, 100)
  assert.equal(done.active, false)
})

test('构建状态文案：摘要与任务标签', () => {
  const m = normalizeBuildStatus({
    total_chunks: 5678,
    indexed_chunks: 1234,
    pending_chunks: 4444,
    dead_chunks: 3,
    build_task_status: 'running',
    build_task_progress: 22
  })
  assert.equal(buildStatusSummary(m), '已索引 1,234 · 共 5,678 段 · 待索引 4,444 · 死信 3')
  assert.equal(buildTaskLabel(m), '正在索引 22%')
  const failed = normalizeBuildStatus({
    total_chunks: 10,
    indexed_chunks: 5,
    build_task_status: 'failed',
    build_task_message: '历史任务未完成：仍有 5 个 Chunk 待索引'
  })
  assert.equal(buildTaskLabel(failed), '历史任务未完成：仍有 5 个 Chunk 待索引')
  const idle = normalizeBuildStatus({ total_chunks: 0, indexed_chunks: 0 })
  assert.equal(buildStatusSummary(idle), '已索引 0 · 共 0 段')
  assert.equal(buildTaskLabel(idle), '')
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) {
  process.exit(1)
}
