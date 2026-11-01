import { apiGet, apiPost } from './base'

export const userApi = {
  messageActivity: (options = {}) => apiGet('/api/user/message-activity', options),
  uploadImage: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    return apiPost('/api/user/upload-image', formData)
  }
}
