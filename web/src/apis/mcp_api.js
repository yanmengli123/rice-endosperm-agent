import { apiGet, apiAdminGet, apiAdminPost, apiAdminPut, apiAdminDelete } from './base'

/**
 * MCP 服务器管理 API 模块
 * 包含 MCP 服务器的增删改查和工具管理功能
 */

const BASE_URL = '/api/system/mcp-servers'

// =============================================================================
// === MCP 服务器 CRUD ===
// =============================================================================

/**
 * 获取所有 MCP 服务器配置
 * @returns {Promise} - 服务器列表
 */
export const getMcpServers = async () => {
  return apiGet(BASE_URL)
}

/**
 * 获取单个 MCP 服务器配置
 * @param {string} name - 服务器名称
 * @returns {Promise} - 服务器配置
 */
export const getMcpServer = async (name) => {
  return apiAdminGet(`${BASE_URL}/${encodeURIComponent(name)}`)
}

/**
 * 创建新的 MCP 服务器
 * @param {Object} data - 服务器配置数据
 * @returns {Promise} - 创建结果
 */
export const createMcpServer = async (data) => {
  return apiAdminPost(BASE_URL, data)
}

/**
 * 更新 MCP 服务器配置
 * @param {string} name - 服务器名称
 * @param {Object} data - 更新数据
 * @returns {Promise} - 更新结果
 */
export const updateMcpServer = async (name, data) => {
  return apiAdminPut(`${BASE_URL}/${encodeURIComponent(name)}`, data)
}

/**
 * 删除 MCP 服务器
 * @param {string} name - 服务器名称
 * @returns {Promise} - 删除结果
 */
export const deleteMcpServer = async (name) => {
  return apiAdminDelete(`${BASE_URL}/${encodeURIComponent(name)}`)
}

// =============================================================================
// === MCP 服务器操作 ===
// =============================================================================

/**
 * 批量导入外部格式 MCP 定义（官方 Registry server.json / Claude-Cursor 配置 / URL）
 * @param {string|Object} payload - 导入内容：JSON 字符串或已解析对象
 * @returns {Promise} - {success, message, data:[{slug,name,status,warnings}]}
 */
export const importMcpConfig = async (payload) => {
  return apiAdminPost(`${BASE_URL}/import`, { payload })
}

/**
 * 读取最近一次结构化健康诊断结果（不触发实时探测）
 * @param {string} name - 服务器名称
 * @returns {Promise} - {success, status, data}
 */
export const getMcpServerHealth = async (name) => {
  return apiAdminGet(`${BASE_URL}/${encodeURIComponent(name)}/health`)
}

/**
 * 测试 MCP 服务器连接
 * @param {string} name - 服务器名称
 * @returns {Promise} - 测试结果（含结构化 health 字段）
 */
export const testMcpServer = async (name) => {
  return apiAdminPost(`${BASE_URL}/${encodeURIComponent(name)}/test`, {})
}

/**
 * 更新 MCP 服务器启用状态
 * @param {string} name - 服务器名称
 * @param {boolean} enabled - 是否启用
 * @returns {Promise} - 切换结果
 */
export const updateMcpServerStatus = async (name, enabled) => {
  return apiAdminPut(`${BASE_URL}/${encodeURIComponent(name)}/status`, { enabled })
}

// =============================================================================
// === MCP 工具管理 ===
// =============================================================================

/**
 * 获取 MCP 服务器的工具列表
 * @param {string} name - 服务器名称
 * @returns {Promise} - 工具列表
 */
export const getMcpServerTools = async (name) => {
  return apiAdminGet(`${BASE_URL}/${encodeURIComponent(name)}/tools`)
}

/**
 * 刷新 MCP 服务器的工具列表（清除缓存重新获取）
 * @param {string} name - 服务器名称
 * @returns {Promise} - 刷新结果
 */
export const refreshMcpServerTools = async (name) => {
  return apiAdminPost(`${BASE_URL}/${encodeURIComponent(name)}/tools/refresh`, {})
}

/**
 * 切换单个工具的启用状态
 * @param {string} serverName - 服务器名称
 * @param {string} toolName - 工具名称
 * @returns {Promise} - 切换结果
 */
export const toggleMcpServerTool = async (serverName, toolName) => {
  return apiAdminPut(
    `${BASE_URL}/${encodeURIComponent(serverName)}/tools/${encodeURIComponent(toolName)}/toggle`,
    {}
  )
}

export const mcpApi = {
  getMcpServers,
  getMcpServer,
  createMcpServer,
  updateMcpServer,
  deleteMcpServer,
  importMcpConfig,
  getMcpServerHealth,
  testMcpServer,
  updateMcpServerStatus,
  getMcpServerTools,
  refreshMcpServerTools,
  toggleMcpServerTool
}

export default mcpApi
