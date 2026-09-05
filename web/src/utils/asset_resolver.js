/**
 * kbasset:// 逻辑资源解析器。
 *
 * 科研 PDF 的图片以私有对象存放在 MinIO，Markdown 只允许携带
 * `kbasset://{file_id}/{revision_id}/{asset_name}` 逻辑 URI。渲染层经
 * 鉴权 Asset API（Bearer）拉取字节并转成 Blob URL 展示，MinIO URL /
 * 预签名 URL 永不进入前端。
 *
 * 生命周期契约（由调用方保证）：
 * - 内容/上下文变化：先 `abortAll()` 取消在途请求，再 `revokeAll()` 释放
 *   旧 Blob URL，最后用新 URI 集 `resolveAssets()`。
 * - 组件卸载：`abortAll()` + `revokeAll()`。
 */

import { documentApi } from '@/apis/knowledge_api'
import { parseKbAssetUri } from '@/utils/kbasset_contract'

const MAX_CONCURRENCY = 5
const ALLOWED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif'])

/**
 * 创建一个独立的解析会话。每个会话持有自己的去重缓存、并发队列、
 * AbortController 与 Blob URL 清单；一次内容切换使用一个新会话。
 */
export function createAssetResolverSession() {
  let controller = new AbortController()
  const blobUrls = new Set()

  const abortAll = () => {
    controller.abort()
    controller = new AbortController()
  }

  const revokeAll = () => {
    for (const url of blobUrls) {
      try {
        URL.revokeObjectURL(url)
      } catch (error) {
        // revoke 失败只影响内存回收，不影响正确性
        console.warn('revokeObjectURL 失败:', error)
      }
    }
    blobUrls.clear()
  }

  const fetchAssetBlob = async (kbId, parsed, signal) => {
    const response = await documentApi.getDocumentAsset(kbId, parsed, signal)
    const mediaType = String(response.headers.get('content-type') || '')
      .split(';', 1)[0]
      .trim()
      .toLowerCase()
    if (!ALLOWED_IMAGE_TYPES.has(mediaType)) {
      throw new Error(`不支持的科研图片类型: ${mediaType || 'unknown'}`)
    }
    const blob = await response.blob()
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const blobUrl = URL.createObjectURL(blob)
    blobUrls.add(blobUrl)
    return blobUrl
  }

  /**
   * 解析 Markdown 中出现的全部 kbasset:// URI。
   *
   * @param {string[]} uris 文档中出现的逻辑资源 URI（重复项会被去重）
   * @param {Object} context 显式渲染上下文（当前仅需 kbId）
   * @returns {Promise<Map<string, string>>} uri -> blob: URL；失败项以 null 占位
   */
  const resolveAssets = async (uris, context = {}) => {
    const unique = [...new Set(uris.filter((uri) => parseKbAssetUri(uri)))]
    const results = new Map(unique.map((uri) => [uri, null]))
    const kbId = String(context.kbId || '').trim()
    if (!unique.length || !kbId) return results

    const queue = [...unique]
    const activeController = controller
    const { signal } = activeController
    const workers = Array.from({ length: Math.min(MAX_CONCURRENCY, queue.length) }, async () => {
      while (queue.length) {
        const uri = queue.shift()
        try {
          results.set(uri, await fetchAssetBlob(kbId, parseKbAssetUri(uri), signal))
        } catch (error) {
          if (signal.aborted) throw error
          console.warn('科研资源解析失败:', uri, error)
        }
      }
    })

    await Promise.all(workers)
    return results
  }

  return {
    resolveAssets,
    abortAll,
    revokeAll,
    get signal() {
      return controller.signal
    }
  }
}

/** 扫描 Markdown 源文本，返回其中出现的全部 kbasset:// URI（按出现顺序）。 */
export const collectAssetUris = (markdown) => {
  const pattern = /kbasset:\/\/[^)\s"']+/g
  const uris = []
  let match
  while ((match = pattern.exec(String(markdown || ''))) !== null) {
    uris.push(match[0])
  }
  return uris
}
