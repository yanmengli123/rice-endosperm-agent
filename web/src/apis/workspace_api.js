import { apiDelete, apiGet, apiPost, apiPut } from './base'
import { useUserStore } from '@/stores/user'

const buildQuery = (params) => {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, String(value))
    }
  })
  return query.toString()
}

export const getWorkspaceTree = (path = '/', recursive = false, filesOnly = false) => {
  const query = buildQuery({ path, recursive, files_only: filesOnly })
  return apiGet(`/api/workspace/tree?${query}`)
}

export const getWorkspaceFileContent = (path) => {
  const query = buildQuery({ path })
  return apiGet(`/api/workspace/file?${query}`, {}, true, 'blob')
}

export const getWorkspaceKnowledgeTree = (kbId, params = {}) => {
  const query = buildQuery({
    kb_id: kbId,
    parent_id: params.parentId,
    path_prefix: params.pathPrefix,
    page: params.page,
    page_size: params.pageSize,
    recursive: params.recursive || false,
    files_only: params.filesOnly || false
  })
  return apiGet(`/api/workspace/knowledge/tree?${query}`)
}

export const getWorkspaceKnowledgeFileContent = (kbId, fileId) => {
  const query = buildQuery({ kb_id: kbId, file_id: fileId })
  return apiGet(`/api/workspace/knowledge/file?${query}`, {}, true, 'blob')
}

export const downloadWorkspaceKnowledgeFile = (kbId, fileId, variant = 'original') => {
  const query = buildQuery({ kb_id: kbId, file_id: fileId, variant })
  return apiGet(`/api/workspace/knowledge/download?${query}`, {}, true, 'blob')
}

/**
 * 带流式进度的知识库原件下载（证据阅读器专用）。
 *
 * 逐块读取响应体并回调 onProgress(received, total)；total 为 0 表示服务端
 * 未提供 Content-Length（此时回调 received/0，调用方只显示已下载量）。
 * signal 用于阅读器关闭时中断大文件下载，避免无谓带宽占用。
 * @returns {Promise<ArrayBuffer>} 文件字节
 */
export const downloadWorkspaceKnowledgeFileWithProgress = async (
  kbId,
  fileId,
  { variant = 'original', onProgress, signal } = {}
) => {
  const query = buildQuery({ kb_id: kbId, file_id: fileId, variant })
  const userStore = useUserStore()
  if (!userStore.isLoggedIn) throw new Error('用户未登录')
  const response = await fetch(`/api/workspace/knowledge/download?${query}`, {
    headers: userStore.getAuthHeaders(),
    signal
  })
  if (!response.ok) {
    let errorMessage = `请求失败: ${response.status}, ${response.statusText}`
    try {
      const errorData = await response.json()
      const detail = errorData?.detail
      errorMessage = (typeof detail === 'string' && detail) || errorData?.message || errorMessage
    } catch {
      /* 非 JSON 错误体保持默认文案 */
    }
    throw new Error(errorMessage)
  }
  const total = Number(response.headers.get('content-length')) || 0
  if (!response.body) {
    const blob = await response.blob()
    onProgress?.(blob.size, blob.size)
    return blob.arrayBuffer()
  }
  const reader = response.body.getReader()
  const chunks = []
  let received = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    received += value.length
    onProgress?.(received, total)
  }
  const merged = new Uint8Array(received)
  let offset = 0
  for (const chunk of chunks) {
    merged.set(chunk, offset)
    offset += chunk.length
  }
  onProgress?.(received, received || total)
  return merged.buffer
}

export const saveWorkspaceFileContent = (path, content) => {
  return apiPut('/api/workspace/file', { path, content })
}

export const deleteWorkspacePath = (path) => {
  const query = buildQuery({ path })
  return apiDelete(`/api/workspace/file?${query}`)
}

export const createWorkspaceDirectory = (parentPath, name) => {
  return apiPost('/api/workspace/directory', {
    parent_path: parentPath,
    name
  })
}

export const uploadWorkspaceFiles = (parentPath, files) => {
  const formData = new FormData()
  formData.append('parent_path', parentPath)
  files.forEach((file) => formData.append('files', file))
  return apiPost('/api/workspace/upload', formData)
}

export const downloadWorkspaceFile = (path) => {
  const query = buildQuery({ path })
  return apiGet(`/api/workspace/download?${query}`, {}, true, 'blob')
}
