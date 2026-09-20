import { apiAdminDelete, apiAdminGet, apiAdminPost, apiAdminPut } from './base'

/**
 * 工具管理 API 模块
 * 内置工具目录（只读）+ 自定义数据面工具 CRUD
 */

const BASE_URL = '/api/system/tools'

/**
 * 获取工具列表（内置 + 本租户自定义工具合并视图）
 * @param {string} category - 可选，按分类筛选（custom 为自定义分组）
 * @returns {Promise} - 工具列表
 */
export const getTools = async (category = null) => {
  const query = category ? `?${new URLSearchParams({ category }).toString()}` : ''
  return apiAdminGet(`${BASE_URL}${query}`)
}

/**
 * 获取工具选项列表（用于下拉选择）
 * @returns {Promise} - 工具选项
 */
export const getToolOptions = async () => {
  return apiAdminGet(`${BASE_URL}/options`)
}

/**
 * 获取自定义工具详情（含 spec/args_schema/last_health）
 * @param {string} slug - 工具标识
 */
export const getCustomTool = async (slug) => {
  return apiAdminGet(`${BASE_URL}/custom/${encodeURIComponent(slug)}`)
}

/**
 * 创建自定义工具（DRAFT 状态，需测连通过后启用）
 * @param {Object} data - 工具定义
 */
export const createTool = async (data) => {
  return apiAdminPost(BASE_URL, data)
}

/**
 * 更新自定义工具；连接配置变更会回 DRAFT 并停用
 * @param {string} slug - 工具标识（不可修改）
 * @param {Object} data - 更新字段
 */
export const updateTool = async (slug, data) => {
  return apiAdminPut(`${BASE_URL}/custom/${encodeURIComponent(slug)}`, data)
}

/**
 * 删除自定义工具（被智能体引用时后端返回 409）
 * @param {string} slug - 工具标识
 */
export const deleteTool = async (slug) => {
  return apiAdminDelete(`${BASE_URL}/custom/${encodeURIComponent(slug)}`)
}

/**
 * 测连：连通（HTTP < 500）置 READY，否则 FAILED
 * @param {string} slug - 工具标识
 * @param {Object} sampleArgs - 可选样例参数
 */
export const testTool = async (slug, sampleArgs = null) => {
  return apiAdminPost(`${BASE_URL}/custom/${encodeURIComponent(slug)}/test`, {
    sample_args: sampleArgs
  })
}

/**
 * 启用/停用自定义工具（启用要求 READY）
 * @param {string} slug - 工具标识
 * @param {boolean} enabled - 是否启用
 */
export const setToolStatus = async (slug, enabled) => {
  return apiAdminPut(`${BASE_URL}/custom/${encodeURIComponent(slug)}/status`, { enabled })
}

/**
 * 从 OpenAPI 文档批量导入 DRAFT 自定义工具
 * @param {Object} payload - { document, base_url?, name_prefix? }
 */
export const importOpenApiTools = async (payload) => {
  return apiAdminPost(`${BASE_URL}/import`, payload)
}

export const toolApi = {
  getTools,
  getToolOptions,
  getCustomTool,
  createTool,
  updateTool,
  deleteTool,
  testTool,
  setToolStatus,
  importOpenApiTools
}

export default toolApi
