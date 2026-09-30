import { apiGet, apiPost, apiPut, apiDelete } from './base'

const CHANNEL_BASE_PATH = '/api/channels'

export const channelApi = {
  listTypes: () => apiGet(`${CHANNEL_BASE_PATH}/types`),

  listApps: () => apiGet(`${CHANNEL_BASE_PATH}/apps`),

  createApp: (data) => apiPost(`${CHANNEL_BASE_PATH}/apps`, data),

  updateApp: (id, data) => apiPut(`${CHANNEL_BASE_PATH}/apps/${id}`, data),

  deleteApp: (id) => apiDelete(`${CHANNEL_BASE_PATH}/apps/${id}`),

  regeneratePathToken: (id) => apiPost(`${CHANNEL_BASE_PATH}/apps/${id}/regenerate-path-token`),

  listMessages: (id, params = {}) => apiGet(`${CHANNEL_BASE_PATH}/apps/${id}/messages`, { params }),

  listOutbox: (id, params = {}) => apiGet(`${CHANNEL_BASE_PATH}/apps/${id}/outbox`, { params }),

  requeueOutbox: (outboxId) => apiPost(`${CHANNEL_BASE_PATH}/outbox/${outboxId}/requeue`),

  listEndUsers: (id) => apiGet(`${CHANNEL_BASE_PATH}/apps/${id}/end-users`),

  unbindEndUser: (appId, endUserId) => apiDelete(`${CHANNEL_BASE_PATH}/apps/${appId}/end-users/${endUserId}`),

  createPairing: (appId) => apiPost(`${CHANNEL_BASE_PATH}/apps/${appId}/pairings`)
}
