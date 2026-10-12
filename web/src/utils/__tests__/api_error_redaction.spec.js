// Exercise the real request wrapper with synthetic credentials and responses.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { transform } from 'esbuild'

const source = await readFile(new URL('../../apis/base.js', import.meta.url), 'utf8')
const { code } = await transform(source, { format: 'cjs' })
// base.js 引入的超时错误归一化（WIP 新增依赖）：预加载真实实现，registry 同步取用
const apiErrorModule = { exports: {} }
vm.runInNewContext(
  (
    await transform(
      await readFile(new URL('../../utils/apiError.js', import.meta.url), 'utf8'),
      { format: 'cjs' }
    )
  ).code,
  {
    module: apiErrorModule,
    exports: apiErrorModule.exports,
    require: () => {
      throw new Error('apiError.js should stay dependency-free')
    }
  }
)
const logs = []
const requests = []
let response
const apiModule = { exports: {} }
vm.runInNewContext(code, {
  module: apiModule,
  exports: apiModule.exports,
  FormData,
  AbortController,
  AbortSignal,
  setTimeout,
  clearTimeout,
  console: Object.fromEntries(
    ['log', 'warn', 'error'].map((kind) => [kind, (...args) => logs.push(args)])
  ),
  fetch: async (url, options) => {
    requests.push({ url, options })
    return response
  },
  require: (id) => {
    if (id === '@/stores/user')
      return {
        useUserStore: () => ({
          isLoggedIn: true,
          getAuthHeaders: () => ({ Authorization: 'Bearer fixture-token-private' })
        })
      }
    if (id === 'ant-design-vue') return { message: { error() {} } }
    if (id === '@/utils/apiError') return apiErrorModule.exports
    throw new Error(`Unexpected import: ${id}`)
  }
})
const { apiPost } = apiModule.exports
response = new Response(
  JSON.stringify({ detail: { message: '输入校验失败', input: 'fixture-response-private' } }),
  {
    status: 422,
    headers: { 'Content-Type': 'application/json' }
  }
)
await assert.rejects(
  apiPost('/api/literature/cards?private=fixture-query-private', {
    content: 'fixture-body-private'
  }),
  (error) => error.message === '输入校验失败' && error.response.status === 422
)
const printed = JSON.stringify(logs)
for (const value of [
  'fixture-token-private',
  'fixture-body-private',
  'fixture-response-private',
  'fixture-query-private'
]) {
  assert.ok(!printed.includes(value), '错误日志不得泄露凭据、文献内容或查询参数')
}
assert.ok(printed.includes('422'), '保留排障所需状态码')

response = new Response('authenticated asset')
const literatureModule = { exports: {} }
vm.runInNewContext(
  (
    await transform(
      await readFile(new URL('../../apis/literature_api.js', import.meta.url), 'utf8'),
      { format: 'cjs' }
    )
  ).code,
  {
    module: literatureModule,
    exports: literatureModule.exports,
    require: () => apiModule.exports
  }
)
const asset = await literatureModule.exports.fetchLiteratureAssetBlob('kbasset://fixture')
assert.equal(await asset.text(), 'authenticated asset')
assert.equal(requests.at(-1).url, '/api/literature/assets?uri=kbasset%3A%2F%2Ffixture')
assert.equal(requests.at(-1).options.headers.Authorization, 'Bearer fixture-token-private')
const file = new FormData()
file.append('file', new Blob(['fixture']))
response = new Response('{}', { headers: { 'Content-Type': 'application/json' } })
await apiPost('/api/literature/papers/a/attachments', file)
assert.equal(requests.at(-1).options.headers['Content-Type'], undefined)
assert.equal(requests.at(-1).options.body, file)
console.log(
  'API error redaction, authenticated binary responses and multipart boundary tests passed'
)
