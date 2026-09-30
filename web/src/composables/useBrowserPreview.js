import { computed, onUnmounted, ref } from 'vue'
import { browserApi } from '@/apis/browser_api'
import { processRunSseResponse } from '@/composables/useAgentRunStream'

// 预览结束原因 → 中文文案（与后端 end.reason 契约对应）
const BROWSER_PREVIEW_END_REASONS = {
  run_terminal: '运行已结束',
  viewer_limit: '同时观看人数已达上限',
  max_duration: '预览已达最大时长',
  bridge_offline: '本机浏览器连接离线'
}

const CONTROL_ACTIONS = new Set(['pause', 'resume', 'end'])

/**
 * 本机浏览器实时预览组合式函数
 *
 * BrowserConnectionPanel（扩展页预览卡）与 AgentChatComponent（聊天内预览条）共用。
 * 通过 fetch + reader 手工解析 SSE（EventSource 无法携带 Authorization 头），
 * 帧协议见 /api/browser/runs/{run_id}/preview：
 *   {"type":"frame","data_url":"data:image/png;base64,..."}
 *   {"type":"heartbeat"}
 *   {"type":"end","reason":"run_terminal|viewer_limit|max_duration|bridge_offline"}
 *   {"type":"error","code":"...","message":"..."}（后跟 end）
 *
 * @param {Object} options
 * @param {number} [options.autoCloseMs=0] - 收到 end 帧后自动关闭的延时（毫秒），
 *   0 表示不自动关闭（扩展页预览卡保持展示结束原因）；聊天内预览条传 8000。
 * @param {Function} [options.onAutoClose=null] - 自动关闭完成后的回调（用于隐藏 UI 条）。
 */
export function useBrowserPreview({ autoCloseMs = 0, onAutoClose = null } = {}) {
  const activeRunId = ref('')
  const starting = ref(false)
  const streaming = ref(false)
  const frameDataUrl = ref('')
  const ended = ref(false)
  const endReason = ref('')
  const error = ref('')
  const controlPending = ref('')

  let abortController = null
  let autoCloseTimer = null

  const endReasonText = computed(
    () => BROWSER_PREVIEW_END_REASONS[endReason.value] || endReason.value
  )

  const clearAutoCloseTimer = () => {
    if (autoCloseTimer) {
      clearTimeout(autoCloseTimer)
      autoCloseTimer = null
    }
  }

  // 中断当前 SSE 连接；不清理画面与结束状态
  const abortStream = () => {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    starting.value = false
    streaming.value = false
  }

  /** 流结束收尾：保留最后一帧与结束原因展示 */
  const finish = (reason = '') => {
    abortStream()
    ended.value = true
    if (reason) endReason.value = reason
    if (autoCloseMs > 0) scheduleAutoClose()
  }

  const scheduleAutoClose = () => {
    clearAutoCloseTimer()
    autoCloseTimer = setTimeout(
      () => {
        autoCloseTimer = null
        closePreview()
        if (typeof onAutoClose === 'function') onAutoClose()
      },
      Math.max(0, autoCloseMs)
    )
  }

  /** 彻底复位（关闭按钮 / 自动关闭 / 组件卸载） */
  const closePreview = () => {
    abortStream()
    clearAutoCloseTimer()
    activeRunId.value = ''
    frameDataUrl.value = ''
    ended.value = false
    endReason.value = ''
    error.value = ''
    controlPending.value = ''
  }

  /**
   * 开始观看指定 run 的实时预览
   * 防重入：同一 runId 已在观看时直接复用当前流，不重复建连。
   * @param {string} runId
   * @returns {Promise<boolean>} 是否成功建立（或复用）预览流
   */
  const startPreview = async (runId) => {
    const normalized = String(runId || '').trim()
    if (!normalized) return false
    if (activeRunId.value === normalized && (starting.value || streaming.value) && !ended.value) {
      return true
    }

    // 切换 run：先停掉旧流再重建
    abortStream()
    clearAutoCloseTimer()
    activeRunId.value = normalized
    frameDataUrl.value = ''
    ended.value = false
    endReason.value = ''
    error.value = ''

    const controller = new AbortController()
    abortController = controller
    starting.value = true
    try {
      const response = await browserApi.streamBrowserRunPreview(normalized, {
        signal: controller.signal
      })
      if (!response.ok) {
        let detail = `预览连接失败（${response.status}）`
        try {
          const data = await response.json()
          const nested = data?.detail
          detail =
            (typeof nested === 'object' && nested?.message) || nested || data?.message || detail
        } catch {
          // 保持默认文案
        }
        throw new Error(String(detail))
      }
      if (abortController !== controller) return true

      streaming.value = true
      await processRunSseResponse(response, (event, data) => {
        // 流已被替换/关闭后到达的帧一律丢弃
        if (!data || abortController !== controller) return
        const type = data.type || event
        if (type === 'frame') {
          if (typeof data.data_url === 'string' && data.data_url) {
            frameDataUrl.value = data.data_url
          }
        } else if (type === 'end') {
          finish(typeof data.reason === 'string' ? data.reason : '')
        } else if (type === 'error') {
          error.value = data.message || data.code || '预览流发生错误'
        }
      })
      // 流自然关闭但未收到 end 帧：按断开处理，保留最后画面
      if (abortController === controller && !ended.value) {
        finish('')
        error.value = error.value || '预览连接已断开'
      }
      return true
    } catch (err) {
      if (err?.name !== 'AbortError' && abortController === controller) {
        console.error('浏览器预览流错误:', err)
        error.value = err?.message || '预览连接失败'
        ended.value = true
        if (autoCloseMs > 0) scheduleAutoClose()
      }
      return false
    } finally {
      if (abortController === controller) {
        abortController = null
        starting.value = false
        streaming.value = false
      }
    }
  }

  /**
   * 向当前 run 发送控制指令（pause/resume/end）
   * @param {string} action
   * @returns {Promise<{ok: boolean, action: string, status: string}>}
   */
  const sendControl = async (action) => {
    if (!CONTROL_ACTIONS.has(action)) {
      throw new Error(`不支持的浏览器控制动作：${action}`)
    }
    if (!activeRunId.value) {
      throw new Error('当前没有进行中的浏览器预览')
    }
    controlPending.value = action
    try {
      const result = await browserApi.controlBrowserRun(activeRunId.value, action)
      if (action === 'end') {
        // 结束指令成功后主动收尾（服务端随后也会推送 end 帧，这里兜底立即停止）
        finish('run_terminal')
      }
      return result
    } finally {
      controlPending.value = ''
    }
  }

  /**
   * 由调用方在 run 终态事件到达时通知（聊天内预览条用它触发延时自动关闭）；
   * autoCloseMs=0（扩展页预览卡）时为空操作。
   */
  const notifyRunTerminal = (runId) => {
    if (!runId || autoCloseMs <= 0) return
    if (activeRunId.value !== String(runId)) return
    if (autoCloseTimer || ended.value) return
    scheduleAutoClose()
  }

  onUnmounted(() => {
    closePreview()
  })

  return {
    // 状态
    activeRunId,
    starting,
    streaming,
    frameDataUrl,
    ended,
    endReason,
    endReasonText,
    error,
    controlPending,
    // 动作
    startPreview,
    closePreview,
    sendControl,
    notifyRunTerminal
  }
}
