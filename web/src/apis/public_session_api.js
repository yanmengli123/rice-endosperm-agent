import { apiRequest } from './base'
import { useUserStore } from '@/stores/user'
import { publicSessionEvents } from '@/utils/publicSessionStream'

const base = '/api/v1/agents'
const request = (path, method = 'GET', body, key) =>
  apiRequest(base + path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: key ? { 'Idempotency-Key': key } : {}
  })

export const publicSessionApi = {
  agents: () => request(''),
  projects: () => request('/projects'),
  createProject: (name) => request('/projects', 'POST', { name }),
  sessions: () => request('/sessions?limit=100'),
  search: (q) => request('/sessions/search?q=' + encodeURIComponent(q)),
  create: (payload, key) => request('/sessions', 'POST', payload, key),
  session: (id) => request(`/sessions/${id}`),
  items: (id, after) =>
    request(`/sessions/${id}/items?limit=100${after ? '&after=' + encodeURIComponent(after) : ''}`),
  queue: (id) => request(`/sessions/${id}/queue`),
  cooperation: (id) => request(`/sessions/${id}/cooperation`),
  event: (id, event, key) => request(`/sessions/${id}/events`, 'POST', { events: [event] }, key),
  viewed: (id) => request(`/sessions/${id}/viewed`, 'POST'),
  archive: (id) => request(`/sessions/${id}/archive`, 'POST'),
  files: (id, path) =>
    request(`/sessions/${id}/files${path ? '?path=' + encodeURIComponent(path) : ''}`),
  attachments: (id) => request(`/sessions/${id}/attachments`),
  deleteAttachment: (id, file) => request(`/sessions/${id}/attachments/${file}`, 'DELETE'),
  upload: (file) => {
    const body = new FormData()
    body.append('file', file)
    return apiRequest(base + '/files', { method: 'POST', body })
  },
  uploadFolderFile: (id, file, relativePath) => {
    const body = new FormData()
    body.append('file', file)
    body.append('relative_path', relativePath)
    return apiRequest(base + `/sessions/${id}/files`, { method: 'POST', body })
  },
  download: (url) => apiRequest(url, {}, true, 'blob'),
  save: (id, path) => request(`/sessions/${id}/artifacts/save`, 'POST', { path }),
  async compress(id, signal) {
    const response = await fetch(base + `/sessions/${id}/compress`, {
      method: 'POST',
      signal,
      headers: useUserStore().getAuthHeaders()
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '压缩失败')
    return data
  },
  async subscribe(id, { signal, after, onEvent, onConnection }) {
    let cursor = after
    let delay = 500
    while (!signal.aborted) {
      try {
        const response = await fetch(base + `/sessions/${id}/events`, {
          signal,
          headers: {
            ...useUserStore().getAuthHeaders(),
            Accept: 'text/event-stream',
            ...(cursor ? { 'Last-Event-ID': cursor } : {})
          }
        })
        if (!response.ok) {
          const error = new Error(`事件订阅失败 (${response.status})`)
          error.fatal = [400, 401, 403, 404, 422].includes(response.status)
          throw error
        }
        delay = 500
        onConnection('已连接')
        for await (const event of publicSessionEvents(response.body)) {
          if (signal.aborted) return
          await onEvent(event)
          if (event.id) cursor = event.id
        }
      } catch (error) {
        if (signal.aborted) return
        if (error.fatal) throw error
        onConnection('正在重连')
      }
      await new Promise((resolve) => {
        const finish = () => {
          clearTimeout(timer)
          signal.removeEventListener('abort', finish)
          resolve()
        }
        const timer = setTimeout(finish, delay)
        signal.addEventListener('abort', finish, { once: true })
      })
      delay = Math.min(delay * 2, 5000)
    }
  }
}
