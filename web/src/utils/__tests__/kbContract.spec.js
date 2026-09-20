/**
 * 契约能力判定测试（裸 node 运行）：node src/utils/__tests__/kbContract.spec.js
 */
import assert from 'node:assert/strict'

import { contractAllows, pickLatestContract } from '../kbContract.js'

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

test('contractAllows：按快照命令判定，快照缺失一律 false（fail-closed）', () => {
  const kb11 = { contract_snapshot: { allowed_commands: ['graph_mindmap_generate', 'stats_repair'] } }
  assert.equal(contractAllows(kb11, 'graph_mindmap_generate'), true)
  assert.equal(contractAllows(kb11, 'sample_questions'), false)
  // 1.0.0 快照无导图命令 → 不显示入口
  const kb10 = { contract_snapshot: { allowed_commands: ['stats_repair'] } }
  assert.equal(contractAllows(kb10, 'graph_mindmap_generate'), false)
  // fail-closed：未加载/损坏的快照
  assert.equal(contractAllows({}, 'stats_repair'), false)
  assert.equal(contractAllows(null, 'stats_repair'), false)
  assert.equal(
    contractAllows({ contract_snapshot: { allowed_commands: 'not-array' } }, 'stats_repair'),
    false
  )
})

test('pickLatestContract：锚定 latest_version，不依赖注册顺序', () => {
  const contracts = [
    { contract_key: 'managed_graph', version: '1.0.0' },
    { contract_key: 'managed_graph', version: '1.1.0', latest_version: '1.1.0' },
    { contract_key: 'generic_document', version: '1.0.0', latest_version: '1.0.0' }
  ]
  const picked = pickLatestContract(contracts, 'managed_graph')
  assert.equal(picked.version, '1.1.0', '注册顺序 1.0.0 在前也必须选 1.1.0')

  // 旧后端快照无 latest_version：客户端 semver 取最大
  const legacy = [
    { contract_key: 'managed_graph', version: '1.0.0' },
    { contract_key: 'managed_graph', version: '1.1.0' }
  ]
  assert.equal(pickLatestContract(legacy, 'managed_graph').version, '1.1.0')

  assert.equal(pickLatestContract(contracts, 'no_such_key'), null)
  assert.equal(pickLatestContract(null, 'managed_graph'), null)
  assert.equal(pickLatestContract([null, { contract_key: 'x' }], 'managed_graph'), null)
})

test('semver 排序正确处理两位/三位版本', () => {
  const contracts = [
    { contract_key: 'k', version: '1.10.0' },
    { contract_key: 'k', version: '1.9.0' }
  ]
  assert.equal(pickLatestContract(contracts, 'k').version, '1.10.0')
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) {
  process.exit(1)
}
