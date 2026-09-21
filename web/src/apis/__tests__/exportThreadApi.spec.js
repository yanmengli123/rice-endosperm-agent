// 回归护栏：会话问答导出按钮的点击链路必须落在 threadApi.exportThreadHtml 上。
// 背景：该方法挂在 threadApi（会话线程域），曾误写成 agentApi.exportThreadHtml —— 静态检查能发现，
//      但真机点击时才会抛 "agentApi.exportThreadHtml is not a function"。这里用 Vite 的 SSR 模块加载器
//      在 Node 里跑真实模块图（含 @ 别名），并断言「导出编排确实调用了 threadApi 上的那个方法」。
// 运行：web 容器内 `docker exec -w /app web-dev node src/apis/__tests__/exportThreadApi.spec.js`
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Node 下加载真实模块图所需的最小宿主环境（组件库用桩，不初始化真实 DOM 组件）
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
globalThis.window = { URL: { createObjectURL: () => 'blob:stub', revokeObjectURL: () => {} } }
globalThis.document = {
  createElement: () => ({ click() {} }),
  body: { appendChild() {}, removeChild() {} }
}

// 本链路只用到 message；真实 ant-design-vue 顶层会碰 DOM 且 SSR 会外部化 node_modules 依赖，
// 所以用 alias 指向临时桩文件（不是插件 resolveId）。
const stubSource = [
  'const noopFactory = () => () => {}',
  'export const message = { loading: noopFactory, success: () => {}, error: () => {}, warning: () => {}, info: () => {} }',
  'export default { message }'
].join('\n')
const stubDir = mkdtempSync(path.join(os.tmpdir(), 'yuxi-antd-stub-'))
const stubFile = path.join(stubDir, 'antd-stub.mjs')
writeFileSync(stubFile, stubSource, 'utf8')

const run = async () => {
  const { createServer } = await import('vite')
  // web/ 根目录（本文件位于 web/src/apis/__tests__/）
  const root = fileURLToPath(new URL('../../..', import.meta.url)).replace(/[\\/]$/, '')
  const server = await createServer({
    configFile: false,
    root,
    resolve: {
      alias: [
        { find: '@', replacement: path.join(root, 'src') },
        { find: /^ant-design-vue$/, replacement: stubFile }
      ]
    },
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'error'
  })

  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    assert.equal(
      typeof threadApi.exportThreadHtml,
      'function',
      'threadApi 必须声明 exportThreadHtml'
    )

    // 打桩断言「导出编排真的调用了它」——换成别的对象名在这里就会失败
    let calledWith = null
    threadApi.exportThreadHtml = (threadId) => {
      calledWith = threadId
      return Promise.resolve({
        blob: async () => new Blob(),
        headers: {
          get: () => "attachment; filename*=UTF-8''%E8%AF%AD%E6%9E%90%E5%AF%B9%E8%AF%9D_ok.html"
        }
      })
    }

    const { useConversationExport } = await server.ssrLoadModule(
      '/src/composables/useConversationExport.js'
    )
    const savedName = await useConversationExport().exportThreadHtml('thread-under-test')

    assert.equal(calledWith, 'thread-under-test', '导出必须调用 threadApi.exportThreadHtml')
    assert.equal(savedName, '语析对话_ok.html', '下载文件名应按 Content-Disposition 解析')

    console.log('exportThreadApi: all assertions passed')
  } finally {
    await server.close()
  }
}

await run()
