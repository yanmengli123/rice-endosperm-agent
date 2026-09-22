import assert from 'node:assert/strict'

import {
  artifactPathsFromChunk,
  artifactsForConversation,
  sessionArtifactGroupsFromMessages
} from '../runArtifacts.js'

const P1 = '/home/gem/user-data/outputs/mcp_results/ricekb_search_1a2b3c4d.json'
const P2 = '/home/gem/user-data/outputs/sequence_deliverables/Os06t0133000-01_cds.fa'

// ── artifactPathsFromChunk ───────────────────────────────────────────────────

assert.strictEqual(artifactPathsFromChunk({}), null)
assert.strictEqual(artifactPathsFromChunk({ artifacts: null }), null)
assert.deepStrictEqual(artifactPathsFromChunk({ artifacts: [] }), [])
assert.deepStrictEqual(artifactPathsFromChunk({ artifacts: [{ virtual_path: P1 }, P2, null] }), [
  P1,
  P2
])

// ── artifactsForConversation：投影键权威（含空），快照兜底，绝不下探线程级 ────

// 1) 投影键存在且非空 → 返回投影
assert.deepStrictEqual(
  artifactsForConversation(
    { messages: [{ type: 'ai', run_id: 'r1', run_artifacts: [{ virtual_path: P1 }] }] },
    {}
  ),
  [P1]
)

// 2) 投影键存在但为空 → 权威空清单，绝不回退快照（跨轮泄漏修复点）
assert.deepStrictEqual(
  artifactsForConversation(
    { messages: [{ type: 'ai', run_id: 'r1', run_artifacts: [] }] },
    { r1: [P1] }
  ),
  []
)

// 3) 投影键缺失（升级前历史数据）→ 回退 run 快照
assert.deepStrictEqual(
  artifactsForConversation({ messages: [{ type: 'ai', run_id: 'r1' }] }, { r1: [P1] }),
  [P1]
)

// 3b) 快照为空数组 → 权威空清单返回 []，不向前找更早 AI 消息的投影
assert.deepStrictEqual(
  artifactsForConversation(
    {
      messages: [
        { type: 'ai', run_id: 'r1', run_artifacts: [P1] },
        { type: 'ai', run_id: 'r2' }
      ]
    },
    { r2: [] }
  ),
  []
)

// 4) 无投影无快照 → 空数组（即使传入线程级累积列表也不读——函数根本不收该参数）
assert.deepStrictEqual(
  artifactsForConversation({ messages: [{ type: 'ai', run_id: 'r3' }] }, {}),
  []
)

// 5) 多个 AI 消息：只看最后一条（最终回答）——更早 AI 消息（重试/中断轮）的
//    产物不属于本轮最终回答，向前找就是同轮跨 run 泄漏
assert.deepStrictEqual(
  artifactsForConversation(
    {
      messages: [
        { type: 'human' },
        { type: 'ai', run_id: 'r1', run_artifacts: [{ virtual_path: P1 }] },
        { type: 'human' },
        { type: 'ai', run_id: 'r2' }
      ]
    },
    {}
  ),
  []
)

// 6) extra_metadata.run_id 兜底取 run_id
assert.deepStrictEqual(
  artifactsForConversation(
    { messages: [{ type: 'ai', extra_metadata: { run_id: 'r1' } }] },
    { r1: [P2] }
  ),
  [P2]
)

// ── sessionArtifactGroupsFromMessages：按轮分组、跨轮去重 ───────────────────

const groups = sessionArtifactGroupsFromMessages([
  { type: 'human' },
  { type: 'ai', run_id: 'r1', run_artifacts: [{ virtual_path: P1 }, P2] },
  { type: 'human' },
  {
    type: 'ai',
    run_id: 'r2',
    run_artifacts: [{ virtual_path: P1 }, { virtual_path: '/x/new.fa' }]
  },
  { type: 'ai', run_id: 'r3' } // 键缺失的历史轮：跳过
])
assert.strictEqual(groups.length, 2)
assert.strictEqual(groups[0].label, '第 1 轮')
assert.deepStrictEqual(groups[0].paths, [P1, P2])
assert.strictEqual(groups[1].label, '第 2 轮')
// P1 已在第 1 轮出现过，跨轮去重后第 2 轮只剩新文件
assert.deepStrictEqual(groups[1].paths, ['/x/new.fa'])
assert.deepStrictEqual(sessionArtifactGroupsFromMessages(null), [])

console.log('runArtifacts: all assertions passed')
