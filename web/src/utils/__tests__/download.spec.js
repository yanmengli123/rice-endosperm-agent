import assert from 'node:assert/strict'

import { parseDownloadFilename } from '../download.js'

const run = () => {
  // 1. 服务端 filename*=UTF-8'' 中文名（URL 编码）→ 解码为可读文件名
  assert.equal(
    parseDownloadFilename(
      "attachment; filename*=UTF-8''%E8%AF%AD%E6%9E%90%E5%AF%B9%E8%AF%9D_RNA-seq_20260921-1737.html"
    ),
    '语析对话_RNA-seq_20260921-1737.html'
  )

  // 2. filename*=UTF-8'' 内的纯 ASCII 名
  assert.equal(
    parseDownloadFilename("attachment; filename*=UTF-8''yuxi-chat.html"),
    'yuxi-chat.html'
  )

  // 3. ASCII 分支：带引号 / 不带引号
  assert.equal(parseDownloadFilename('attachment; filename="yuxi-chat.html"'), 'yuxi-chat.html')
  assert.equal(parseDownloadFilename('attachment; filename=yuxi-chat.html'), 'yuxi-chat.html')

  // 4. filename* 百分号编码非法 → 回退 ASCII 分支，绝不返回 `*=` 残片
  assert.equal(
    parseDownloadFilename('attachment; filename*=UTF-8\'\'%E0%A4%A; filename="fallback.html"'),
    'fallback.html'
  )

  // 5. 只有非法 filename* 且无 ASCII 兜底 → 空串（调用方用默认名）
  assert.equal(parseDownloadFilename("attachment; filename*=UTF-8''%E0%A4%A"), '')

  // 6. 头部缺失 / 空值
  assert.equal(parseDownloadFilename(''), '')
  assert.equal(parseDownloadFilename(undefined), '')

  console.log('download parseDownloadFilename: all assertions passed')
}

run()
