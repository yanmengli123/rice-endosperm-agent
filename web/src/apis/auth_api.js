/**
 * 认证相关 API
 */

import { apiAdminGet, apiAdminPost, apiAdminPut, apiGet, apiPost } from './base'

async function parseErrorDetail(response, fallbackMessage) {
  const contentType = response.headers.get('content-type') || ''

  if (contentType.includes('application/json')) {
    const error = await response.json()
    return error?.detail || fallbackMessage
  }

  const text = (await response.text()).trim()
  return text || fallbackMessage
}

/**
 * 获取 OIDC 配置
 * @returns {Promise<{enabled: boolean, provider_name?: string}>}
 */
async function getOIDCConfig() {
  const response = await fetch('/api/auth/oidc/config')
  if (!response.ok) {
    throw new Error('获取 OIDC 配置失败')
  }
  return response.json()
}

async function getRegisterConfig() {
  const response = await fetch('/api/auth/register-config')
  if (!response.ok) {
    throw new Error('获取注册配置失败')
  }
  return response.json()
}

async function register(payload) {
  const response = await fetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response, '注册失败'))
  }
  return response.json()
}

/**
 * 获取 OIDC 登录 URL
 * @param {string} redirectPath - 登录后的重定向路径
 * @returns {Promise<{login_url: string}>}
 */
async function getOIDCLoginUrl(redirectPath = '/') {
  const params = new URLSearchParams({ redirect_path: redirectPath })
  const response = await fetch(`/api/auth/oidc/login-url?${params}`)
  if (!response.ok) {
    const detail = await parseErrorDetail(response, '获取 OIDC 登录地址失败')
    throw new Error(detail)
  }
  return response.json()
}

/**
 * 使用一次性 code 交换 OIDC 登录结果
 * @param {string} code - 一次性登录 code
 * @returns {Promise<{
 *   access_token: string,
 *   token_type: string,
 *   user_id: number,
 *   username: string,
 *   uid: string,
 *   phone_number: string | null,
 *   avatar: string | null,
 *   role: string,
 *   department_id: number | null,
 *   department_name: string | null
 * }>}
 */
async function getUserAccessOptions() {
  return apiAdminGet('/api/auth/users/access-options')
}

async function exchangeOIDCCode(code) {
  const response = await fetch('/api/auth/oidc/exchange-code', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ code })
  })

  if (!response.ok) {
    const detail = await parseErrorDetail(response, 'OIDC 登录失败')
    throw new Error(detail)
  }

  return response.json()
}

async function getCLIAuthSession(userCode) {
  const encoded = encodeURIComponent(userCode)
  return apiGet(`/api/auth/cli/sessions/${encoded}`)
}

async function approveCLIAuthSession(userCode) {
  const encoded = encodeURIComponent(userCode)
  return apiPost(`/api/auth/cli/sessions/${encoded}/approve`, {})
}

async function listManagedUsers(skip = 0, limit = 100) {
  return apiAdminGet(`/api/auth/users?skip=${skip}&limit=${limit}`)
}

async function createManagedUser(payload) {
  return apiAdminPost('/api/auth/users', payload)
}

async function setManagedUserEnabled(uid, enabled) {
  return apiAdminPost(`/api/user/manage/${encodeURIComponent(uid)}/${enabled ? 'enable' : 'disable'}`)
}

async function getManagedUserQuota(uid) {
  return apiAdminGet(`/api/user/manage/${encodeURIComponent(uid)}/quota`)
}

async function setManagedUserQuota(uid, payload) {
  return apiAdminPut(`/api/user/manage/${encodeURIComponent(uid)}/quota`, payload)
}

async function listManagedUserConversations(uid, page = 1, pageSize = 20) {
  return apiAdminGet(
    `/api/user/manage/${encodeURIComponent(uid)}/conversations?page=${page}&page_size=${pageSize}`
  )
}

async function listManagedUserMessages(uid, threadId) {
  return apiAdminGet(
    `/api/user/manage/${encodeURIComponent(uid)}/conversations/${encodeURIComponent(threadId)}/messages`
  )
}


async function listManagedApiKeys(uid) {
  const data = await apiAdminGet(`/api/user/manage/${encodeURIComponent(uid)}/api-keys`)
  return data.keys
}

async function resetManagedApiKey(uid, keyId) {
  return apiAdminPost(
    `/api/user/manage/${encodeURIComponent(uid)}/api-keys/${keyId}/reset`
  )
}

async function deleteManagedApiKey(uid, keyId) {
  return apiAdminDelete(`/api/user/manage/${encodeURIComponent(uid)}/api-keys/${keyId}`)
}

async function getManagedUserStats(uid, days = 14) {
  return apiAdminGet(`/api/user/manage/${encodeURIComponent(uid)}/stats?days=${days}`)
}

async function resetManagedUserPassword(uid, newPassword) {
  return apiAdminPut(`/api/auth/users/${encodeURIComponent(uid)}/password`, {
    password: newPassword
  })
}

export const authApi = {
  getRegisterConfig,
  register,
  getOIDCConfig,
  getOIDCLoginUrl,
  getUserAccessOptions,
  exchangeOIDCCode,
  getCLIAuthSession,
  approveCLIAuthSession,
  listManagedUsers,
  createManagedUser,
  setManagedUserEnabled,
  getManagedUserQuota,
  setManagedUserQuota,
  listManagedUserConversations,
  listManagedUserMessages,
  listManagedApiKeys,
  resetManagedApiKey,
  deleteManagedApiKey,
  getManagedUserStats,
  resetManagedUserPassword
}
