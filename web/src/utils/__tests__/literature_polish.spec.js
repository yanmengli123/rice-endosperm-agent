// 企业级打磨改动的纯函数测试：任务错误人话映射、事件时间戳取值、目录重复计数。
// Hermetic：无浏览器、无网络、无凭据（与 literature_events.spec.js 同模式）。
import assert from 'node:assert/strict'

import {
  createLiteratureEventBuffer,
  diffTasksToEvents,
  humanizeTaskError
} from '../../composables/useLiteratureEvents.js'
import { duplicateCounts } from '../literature_catalog.js'

// ── humanizeTaskError：已知模式 → 可行动文案；未知 → 剥异常类前缀保留原文 ──
assert.equal(
  humanizeTaskError('ModelCallError: Error calling model: APIConnectionError'),
  '模型服务连接失败：请检查模型配置或供应商状态，稍后可重试'
)
assert.equal(
  humanizeTaskError('WORKER_INTERRUPTED: 执行进程中断，等待重试'),
  '执行进程中断：任务会自动重试或可手动重新入队'
)
assert.equal(
  humanizeTaskError(
    "上传文件 'tenants/1/documents/x.pdf' 失败: S3 operation failed; code: XMinioStorageFull"
  ),
  '对象存储空间不足：请联系管理员扩容后重试'
)
assert.equal(
  humanizeTaskError('ValueError: 当前版本 L1 笔记缺失，无法编译 L3（先编译 L1）'),
  '当前版本 L1 笔记缺失，无法编译 L3（先编译 L1）',
  '未命中映射时剥掉异常类前缀，正文原样保留'
)
assert.equal(humanizeTaskError('boom'), 'boom', '无前缀的原始串不动')
assert.equal(humanizeTaskError(''), '', '空串原样')
assert.equal(humanizeTaskError(null), '', 'null 安全')

// ── 事件时间戳：取任务 updated_at，不再整批同一秒 ──
{
  const seen = []
  const record = (level, source, message, ts, detail) => seen.push({ level, message, ts, detail })
  diffTasksToEvents(
    null,
    [
      {
        type: 'compile',
        ref: '8:L1',
        status: 'DEAD',
        attempt: 1,
        error: 'ModelCallError: Error calling model: APIConnectionError',
        updated_at: '2026-10-05T10:00:00Z'
      },
      {
        type: 'ingest',
        ref: 'spr_x',
        status: 'REJECTED',
        attempt: 0,
        updated_at: '2026-10-05T12:30:00Z'
      }
    ],
    record,
    1_900_000_000_000
  )
  assert.equal(seen.length, 2)
  assert.equal(seen[0].ts, Date.parse('2026-10-05T10:00:00Z'), '事件时间用任务 updated_at')
  assert.equal(seen[1].ts, Date.parse('2026-10-05T12:30:00Z'), '两条事件时间各自独立')
  assert.ok(seen[0].message.includes('模型服务连接失败'), '错误消息为人话文案')
  assert.ok(seen[0].detail.includes('ModelCallError'), '原始错误串进 detail')
  assert.equal(seen[1].detail, '', '非失败事件无 detail')
}
{
  // 无 updated_at 的旧载荷回退轮询时刻
  const seen = []
  diffTasksToEvents(
    null,
    [{ type: 'translation', ref: 'old:zh', status: 'FAILED', attempt: 0 }],
    (level, source, message, ts) => seen.push(ts),
    1_900_000_000_001
  )
  assert.equal(seen[0], 1_900_000_000_001, '缺失 updated_at 时回退传入时刻')
}

// ── 事件缓冲 detail 字段：存取与去重不受影响 ──
{
  const buffer = createLiteratureEventBuffer(5)
  buffer.record('error', '任务', 'msg', 123, 'raw stack')
  assert.equal(buffer.events[0].detail, 'raw stack')
  buffer.record('error', '任务', 'msg', 124, 'raw stack')
  assert.equal(buffer.events.length, 1, '去重仍按 level/source/message')
}

// ── duplicateCounts：同 DOI 优先，其次规范化标题；空标识不参与 ──
{
  const counts = duplicateCounts([
    { rid: 'a', doi: '10.1/x', title: 'Paper One' },
    { rid: 'b', doi: '10.1/X ', title: 'Other' },
    { rid: 'c', title: 'Starch synthesis!' },
    { rid: 'd', title: 'starch   synthesis' },
    { rid: 'e', title: '独立标题' },
    { rid: 'f', title: '' }
  ])
  assert.equal(counts.get('a'), 2, 'DOI 大小写/空白归一后计 2')
  assert.equal(counts.get('b'), 2)
  assert.equal(counts.get('c'), 2, '无 DOI 时按规范化标题（大小写/标点/空白）计 2')
  assert.equal(counts.get('d'), 2)
  assert.equal(counts.get('e'), 1, '唯一条目计 1')
  assert.equal(counts.get('f'), 1, '无 DOI 且标题规范化后为空：不参与检测')
  assert.equal(duplicateCounts(null).size, 0, '空目录安全')
  assert.equal(duplicateCounts([]).size, 0)
}

console.log('Literature enterprise polish (humanizer, event ts, duplicates) passed')
