import { apiGet, apiPost, apiDelete, apiAdminGet, apiAdminPut } from './base'
import { useUserStore } from '@/stores/user'

/**
 * 本机浏览器接入API模块
 * 包含浏览器扩展配对链接、连接状态与设备管理等功能
 * 权限要求: 任何已登录用户（普通用户、管理员、超级管理员）
 * 域名策略接口仅管理员可用（非管理员服务端返回 403）
 */

export const browserApi = {
  /**
   * 创建浏览器扩展配对链接（5 分钟有效）
   * @returns {Promise<{code: string, pairing_url: string, expires_at: string}>}
   */
  createPairingLink: () => apiPost('/api/browser/pairing-links', {}),

  /**
   * 获取本机浏览器连接状态
   * @returns {Promise<{paired: boolean, online: boolean, device: Object|null}>}
   */
  getBrowserStatus: () => apiGet('/api/browser/status'),

  /**
   * 获取已授权的浏览器设备列表
   * @returns {Promise<{devices: Array<Object>}>}
   */
  listBrowserDevices: () => apiGet('/api/browser/devices'),

  /**
   * 撤销指定浏览器设备的授权
   * @param {string} deviceId - 设备ID
   * @returns {Promise<{ok: boolean}>}
   */
  revokeBrowserDevice: (deviceId) =>
    apiDelete(`/api/browser/devices/${encodeURIComponent(deviceId)}`),

  /**
   * 获取浏览器导航域名策略（仅管理员）
   * @returns {Promise<{mode: string, domains: Array<string>, updated_at: string}>}
   */
  getBrowserPolicy: () => apiAdminGet('/api/browser/policy'),

  /**
   * 更新浏览器导航域名策略（仅管理员）；mode=off 时 domains 忽略，可传空数组
   * @param {{mode: string, domains: Array<string>}} policy - 策略配置
   * @returns {Promise<{mode: string, domains: Array<string>, updated_at: string}>}
   */
  updateBrowserPolicy: (policy) => apiAdminPut('/api/browser/policy', policy),

  /**
   * 对指定运行发送浏览器控制指令（pause/resume/end）
   * @param {string} runId - 运行ID
   * @param {string} action - 控制动作：pause | resume | end
   * @returns {Promise<{ok: boolean, action: string, status: string}>}
   */
  controlBrowserRun: (runId, action) =>
    apiPost(`/api/browser/runs/${encodeURIComponent(runId)}/control`, { action }),

  /**
   * 打开浏览器运行实时预览 SSE 连接（调用方负责关闭）
   * EventSource 不支持自定义 Authorization 头，必须用 fetch 返回原始 Response，
   * 由调用方配合 processRunSseResponse 手工解析。
   * @param {string} runId - 运行ID
   * @param {Object} options - { signal }
   * @returns {Promise<Response>}
   */
  streamBrowserRunPreview: (runId, options = {}) => {
    const { signal } = options
    const headers = {
      Accept: 'text/event-stream',
      ...useUserStore().getAuthHeaders()
    }
    return fetch(`/api/browser/runs/${encodeURIComponent(runId)}/preview`, {
      method: 'GET',
      headers,
      signal
    })
  }
}
