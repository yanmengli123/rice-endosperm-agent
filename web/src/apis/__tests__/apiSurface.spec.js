import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * API surface 静态检查：捕获「组件调用 XxxApi.method()，但该方法定义在另一个 Api 对象里」
 * 这一类缺陷。
 *
 * 回归背景：DatasetImportPanel 曾调用 `databaseApi.previewCsvDataset`，而该方法实际定义在
 * `typeApi` 中，导致点击「数据集导入」必现 `TypeError: databaseApi.previewCsvDataset is not
 * a function`（前端无任何测试覆盖调用路径，后端单测全绿也没拦住）。
 *
 * 本文件为纯静态检查（只读源码、不 import 业务模块），可用裸 node 直接运行：
 *   docker exec -w /app web-dev node src/apis/__tests__/apiSurface.spec.js
 */

const here = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(here, '..', '..') // web/src
const APIS_DIR = path.join(SRC, 'apis')

function listFiles(dir, extensions) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue
      out.push(...listFiles(full, extensions))
    } else if (extensions.some((ext) => entry.name.endsWith(ext))) {
      out.push(full)
    }
  }
  return out
}

/** 解析 apis/ 下所有 `export const XxxApi = { ... }` 对象的成员名。 */
function parseApiObjects() {
  const objects = new Map()
  for (const file of listFiles(APIS_DIR, ['.js'])) {
    const code = fs.readFileSync(file, 'utf8')
    const declaration = /export const (\w+Api)\s*=\s*\{/g
    let match
    while ((match = declaration.exec(code))) {
      const rest = code.slice(match.index)
      const endIndex = rest.search(/\n\}/)
      const block = endIndex === -1 ? rest : rest.slice(0, endIndex)
      const members = new Set()
      // 兼容三种成员写法：
      //   1) 属性式   `foo: async () => {}`
      //   2) 简写方法 `async foo() {}`
      //   3) 简写属性 `foo,`（先 export const foo，再在对象里引用）
      const member = /^\s{2}(?:async\s+)?([A-Za-z_$][\w$]*)\s*(?=[:(,]|\r?$)/gm
      let hit
      while ((hit = member.exec(block))) members.add(hit[1])
      objects.set(match[1], { file: path.relative(SRC, file), members })
    }
  }
  return objects
}

const apiObjects = parseApiObjects()
assert.ok(apiObjects.size > 0, '未解析到任何 Api 对象，检查解析逻辑')

// --- 检查 1：每个调用点的方法都必须定义在其被使用的那个 Api 对象上 -----------------
const missing = []
for (const file of listFiles(SRC, ['.vue', '.js'])) {
  if (file.startsWith(APIS_DIR)) continue
  const code = fs.readFileSync(file, 'utf8')
  for (const [name, info] of apiObjects) {
    const usage = new RegExp(`\\b${name}\\.([A-Za-z_$][\\w$]*)`, 'g')
    let hit
    while ((hit = usage.exec(code))) {
      if (!info.members.has(hit[1])) {
        const line = code.slice(0, hit.index).split('\n').length
        missing.push(
          `${path.relative(SRC, file)}:${line}  ${name}.${hit[1]}（未定义于 ${info.file} 的 ${name}）`
        )
      }
    }
  }
}
assert.deepEqual(
  missing,
  [],
  `发现「方法挂错对象/方法名拼错」的调用点：\n  ${missing.join('\n  ')}`
)

// --- 检查 2：CSV 数据集方法只能挂在 datasetApi，且只有一份定义 ----------------------
const datasetApi = apiObjects.get('datasetApi')
assert.ok(datasetApi, '缺少 datasetApi 导出（CSV 数据集预检/导入分组）')
const datasetMethods = ['previewCsvDataset', 'importCsvDataset']
for (const method of datasetMethods) {
  assert.ok(datasetApi.members.has(method), `datasetApi 缺少 ${method}`)
  for (const other of ['typeApi', 'databaseApi']) {
    const otherApi = apiObjects.get(other)
    assert.ok(
      !otherApi.members.has(method),
      `${other} 不应再承载数据集方法 ${method}（应归 datasetApi，避免重复定义漂移）`
    )
  }
}

// --- 检查 3：调用方导入的分组必须与归属对象一致 ------------------------------------
const panelPath = path.join(SRC, 'components', 'DatasetImportPanel.vue')
const panel = fs.readFileSync(panelPath, 'utf8')
assert.match(
  panel,
  /import \{[^}]*\bdatasetApi\b[^}]*\} from '@\/apis\/knowledge_api'/,
  'DatasetImportPanel 必须从 @/apis/knowledge_api 导入 datasetApi'
)
assert.doesNotMatch(panel, /\bdatabaseApi\b/, 'DatasetImportPanel 不应再引用 databaseApi')

console.log(`[apiSurface] 解析 Api 对象 ${apiObjects.size} 个，全部调用点方法均已定义`)
console.log(`[apiSurface] datasetApi 成员：${[...datasetApi.members].join(', ')}`)
console.log('[apiSurface] OK')
