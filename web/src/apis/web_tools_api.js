import { apiAdminGet, apiAdminPut, apiAdminPost } from './base'
const path = '/api/system/web-tools'
export const webToolsApi = {
  get: () => apiAdminGet(path),
  save: (value) => apiAdminPut(path, value),
  test: (value) => apiAdminPost(`${path}/test`, value)
}
