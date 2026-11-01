import { apiGet, apiPost, apiPut } from './base'

const path = '/api/user/apps'
export const publicAppApi = {
  list: () => apiGet(path),
  usage: (days = 30, options = {}) => apiGet(`${path}/usage`, { ...options, params: { days } }),
  options: () => apiGet(`${path}/grant-options`),
  create: (data) => apiPost(path, data),
  update: (id, data) => apiPut(`${path}/${encodeURIComponent(id)}`, data),
  keys: (id) => apiGet(`${path}/${encodeURIComponent(id)}/keys`),
  issueKey: (id, data) => apiPost(`${path}/${encodeURIComponent(id)}/keys`, data),
  setKey: (id, key, enabled) =>
    apiPut(`${path}/${encodeURIComponent(id)}/keys/${encodeURIComponent(key)}`, { enabled }),
  endUsers: (id) => apiGet(`${path}/${encodeURIComponent(id)}/end-users`),
  setEndUser: (id, uid, enabled) =>
    apiPut(`${path}/${encodeURIComponent(id)}/end-users/${encodeURIComponent(uid)}`, { enabled })
}
