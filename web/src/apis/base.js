import { useUserStore, checkAdminPermission, checkSuperAdminPermission } from '@/stores/user'
import { message } from 'ant-design-vue'
import { normalizeTimeoutError } from '@/utils/apiError'

/**
 * 基础API请求封装
 * 提供统一的请求方法，自动处理认证头和错误
 */

// 非流式请求统一超时：网络挂起时及时失败，避免 UI 无限等待。
// SSE 流式请求走独立 fetch（agent_api），不经过这里。上传与大文件下载放宽到 120s。
const DEFAULT_TIMEOUT_MS = 30000
const SLOW_TIMEOUT_MS = 120000

function requestTimeoutController(isFormData, responseType, callerSignal) {
  const controller = new AbortController()
  let timer
  const clear = () => {
    clearTimeout(timer)
    callerSignal?.removeEventListener('abort', relayAbort)
  }
  const relayAbort = () => {
    controller.abort(callerSignal.reason)
    clear()
  }
  timer = setTimeout(() => {
    controller.abort(new DOMException('请求超时', 'TimeoutError'))
    clear()
  }, isFormData || responseType === 'blob' ? SLOW_TIMEOUT_MS : DEFAULT_TIMEOUT_MS)
  if (callerSignal?.aborted) relayAbort()
  else callerSignal?.addEventListener('abort', relayAbort, { once: true })
  return { signal: controller.signal, clear }
}

/**
 * 发送API请求的基础函数
 * @param {string} url - API端点
 * @param {Object} options - 请求选项（支持 params：序列化为 query string 并入 URL）
 * @param {boolean} requiresAuth - 是否需要认证头
 * @param {string} responseType - 响应类型: 'json' | 'text' | 'blob'
 * @returns {Promise} - 请求结果
 */
/**
 * 把 options.params 序列化为 query string 合并进 URL。
 * null/undefined 值跳过；数组按 key=a&key=b 展开；已有 "?" 用 "&" 拼接。
 */
function buildUrlWithParams(url, params) {
  if (!params) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== null && item !== undefined) search.append(key, item)
      }
    } else {
      search.append(key, value)
    }
  }
  const queryString = search.toString()
  if (!queryString) return url
  return url + (url.includes('?') ? '&' : '?') + queryString
}

export async function apiRequest(url, options = {}, requiresAuth = true, responseType = 'json') {
  const isFormData = options?.body instanceof FormData
  // 生命周期取消与请求期限同时生效；一次挂起不能永久占住共享轮询。
  const timeout = requestTimeoutController(isFormData, responseType, options?.signal)
  let response
  let deferredBody = false
  try {
    // 默认请求配置（params 已并入 URL，不再透传给 fetch）
    const { params: queryParams, ...restOptions } = options || {}
    const requestOptions = {
      ...restOptions,
      signal: timeout.signal,
      headers: {
        ...(!isFormData ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers
      }
    }

    // 如果需要认证，添加认证头
    if (requiresAuth) {
      const userStore = useUserStore()
      if (!userStore.isLoggedIn) {
        throw new Error('用户未登录')
      }

      Object.assign(requestOptions.headers, userStore.getAuthHeaders())
    }

    // 发送请求
    response = await fetch(buildUrlWithParams(url, queryParams), requestOptions)
    if (timeout.signal.aborted) throw timeout.signal.reason

    // 处理API返回的错误
    if (!response.ok) {
      // 尝试解析错误信息
      let errorMessage = `请求失败: ${response.status}, ${response.statusText}`
      let errorData = null

      console.log('API请求失败:', {
        method: requestOptions.method || 'GET',
        status: response.status
      })

      try {
        errorData = await response.json()
        // detail 可能是字符串，也可能是结构化对象（如 { error, message }），后者需取出可读文案，
        // 否则直接拼接会得到 "[object Object]"。
        const detail = errorData.detail
        if (detail && typeof detail === 'object') {
          errorMessage = detail.message || detail.error || errorMessage
        } else {
          errorMessage = detail || errorData.message || errorMessage
        }
      } catch {
        // 如果无法解析JSON，使用默认错误信息
        console.log('无法解析错误响应JSON:', { status: response.status })
      }

      // 已取消的旧请求不得继续触发当前账号的登出等副作用。
      if (timeout.signal.aborted) throw timeout.signal.reason
      // 特殊处理401和403错误
      const error = new Error(errorMessage)
      error.response = {
        status: response.status,
        statusText: response.statusText,
        data: errorData
      }

      if (response.status === 401) {
        // 如果是认证失败，可能需要重新登录
        const userStore = useUserStore()

        // 检查是否是token过期（errorMessage 已统一为字符串，避免对对象 detail 调用 includes 抛错）
        const isTokenExpired =
          errorMessage?.includes('令牌已过期') || errorMessage?.includes('token expired')

        message.error(isTokenExpired ? '登录已过期，请重新登录' : '认证失败，请重新登录')

        // 如果用户当前认为自己已登录，则登出
        if (userStore.isLoggedIn) {
          userStore.logout()
        }

        // 使用setTimeout确保消息显示后再跳转
        setTimeout(() => {
          window.location.href = '/login'
        }, 1500)

        throw error
      } else if (response.status === 403) {
        error.message = '没有权限执行此操作'
        throw error
      } else if (response.status === 500) {
        error.message = '服务器内部错误，请使用 docker logs api-dev 查看详细日志'
        throw error
      }

      throw error
    }

    // 根据responseType处理响应
    if (responseType === 'blob') {
      // 保持原生 Response/headers；字节仍在传输时，调用方取消与期限不能提前解绑。
      deferredBody = true
      for (const method of ['arrayBuffer', 'blob', 'bytes', 'formData', 'json', 'text']) {
        if (typeof response[method] !== 'function') continue
        const consume = response[method].bind(response)
        response[method] = async (...args) => {
          try {
            return await consume(...args)
          } catch (error) {
            throw normalizeTimeoutError(timeout.signal.reason?.name === 'TimeoutError' ? timeout.signal.reason : error)
          } finally {
            timeout.clear()
          }
        }
      }
      return response
    } else if (responseType === 'json') {
      // 检查Content-Type以确定如何处理响应
      const contentType = response.headers.get('Content-Type')
      if (contentType && contentType.includes('application/json')) {
        return await response.json()
      }
      return await response.text()
    } else if (responseType === 'text') {
      return await response.text()
    } else {
      return response
    }
  } catch (error) {
    if (error?.name === 'TimeoutError' || timeout.signal.reason?.name === 'TimeoutError') {
      // DOMException.message is read-only in browsers. Mutating it masks the
      // timeout with a TypeError and prevents the drawer from explaining it.
      throw normalizeTimeoutError(timeout.signal.reason?.name === 'TimeoutError' ? timeout.signal.reason : error)
    } else if (error?.name !== 'AbortError') {
      console.error('API请求错误:', {
        method: options.method || 'GET',
        status: error.response?.status || null,
        type: error.name
      })
    }
    throw error
  } finally {
    if (!deferredBody) timeout.clear()
  }
}

/**
 * 发送GET请求
 * @param {string} url - API端点
 * @param {Object} options - 请求选项
 * @param {boolean} requiresAuth - 是否需要认证
 * @param {string} responseType - 响应类型: 'json' | 'text' | 'blob'
 * @returns {Promise} - 请求结果
 */
export function apiGet(url, options = {}, requiresAuth = true, responseType = 'json') {
  return apiRequest(url, { method: 'GET', ...options }, requiresAuth, responseType)
}

export function apiAdminGet(url, options = {}, responseType = 'json') {
  checkAdminPermission()
  return apiGet(url, options, true, responseType)
}

export function apiSuperAdminGet(url, options = {}, responseType = 'json') {
  checkSuperAdminPermission()
  return apiGet(url, options, true, responseType)
}

/**
 * 发送POST请求
 * @param {string} url - API端点
 * @param {Object} data - 请求体数据
 * @param {Object} options - 其他请求选项
 * @param {boolean} requiresAuth - 是否需要认证
 * @param {string} responseType - 响应类型: 'json' | 'text' | 'blob'
 * @returns {Promise} - 请求结果
 */
export function apiPost(url, data = {}, options = {}, requiresAuth = true, responseType = 'json') {
  return apiRequest(
    url,
    {
      method: 'POST',
      body: data instanceof FormData ? data : JSON.stringify(data),
      ...options
    },
    requiresAuth,
    responseType
  )
}

export function apiAdminPost(url, data = {}, options = {}, responseType = 'json') {
  checkAdminPermission()
  return apiPost(url, data, options, true, responseType)
}

export function apiSuperAdminPost(url, data = {}, options = {}, responseType = 'json') {
  checkSuperAdminPermission()
  return apiPost(url, data, options, true, responseType)
}

/**
 * 发送PUT请求
 * @param {string} url - API端点
 * @param {Object} data - 请求体数据
 * @param {Object} options - 其他请求选项
 * @param {boolean} requiresAuth - 是否需要认证
 * @param {string} responseType - 响应类型: 'json' | 'text' | 'blob'
 * @returns {Promise} - 请求结果
 */
export function apiPut(url, data = {}, options = {}, requiresAuth = true, responseType = 'json') {
  return apiRequest(
    url,
    {
      method: 'PUT',
      body: data instanceof FormData ? data : JSON.stringify(data),
      ...options
    },
    requiresAuth,
    responseType
  )
}

export function apiAdminPut(url, data = {}, options = {}, responseType = 'json') {
  checkAdminPermission()
  return apiPut(url, data, options, true, responseType)
}

export function apiSuperAdminPut(url, data = {}, options = {}, responseType = 'json') {
  checkSuperAdminPermission()
  return apiPut(url, data, options, true, responseType)
}

export function apiPatch(url, data = {}, options = {}, requiresAuth = true, responseType = 'json') {
  return apiRequest(
    url,
    {
      method: 'PATCH',
      body: data instanceof FormData ? data : JSON.stringify(data),
      ...options
    },
    requiresAuth,
    responseType
  )
}

/**
 * 发送DELETE请求
 * @param {string} url - API端点
 * @param {Object} options - 请求选项
 * @param {boolean} requiresAuth - 是否需要认证
 * @param {string} responseType - 响应类型: 'json' | 'text' | 'blob'
 * @returns {Promise} - 请求结果
 */
export function apiDelete(url, options = {}, requiresAuth = true, responseType = 'json') {
  return apiRequest(url, { method: 'DELETE', ...options }, requiresAuth, responseType)
}

export function apiAdminDelete(url, options = {}) {
  checkAdminPermission()
  return apiDelete(url, options, true)
}

export function apiSuperAdminDelete(url, options = {}) {
  checkSuperAdminPermission()
  return apiDelete(url, options, true)
}
