// 会话问答 HTML 导出的公共编排：侧边栏会话菜单与对话页头部「导出」按钮共用同一套逻辑。
// 服务端渲染自包含美化 HTML（GET /api/chat/thread/{thread_id}/export），前端只负责带鉴权下载。
import { ref } from 'vue'
import { message } from 'ant-design-vue'
import { threadApi } from '@/apis/agent_api'
import { saveBlobResponse } from '@/utils/download'

const DEFAULT_FILENAME = '语析对话.html'

// 模块级共享：多个入口（侧边栏菜单 / 对话页按钮）同时触发也只发一次请求
const exportingThreadIds = ref(new Set())

const addExporting = (threadId) => {
  exportingThreadIds.value = new Set(exportingThreadIds.value).add(threadId)
}

const removeExporting = (threadId) => {
  const next = new Set(exportingThreadIds.value)
  next.delete(threadId)
  exportingThreadIds.value = next
}

export function useConversationExport() {
  const isExporting = (threadId) => exportingThreadIds.value.has(threadId)

  /**
   * 导出指定会话的问答记录为美化 HTML 并落盘。
   * @returns {Promise<string>} 实际保存的文件名；未导出（无 id / 正在导出 / 失败）时为空串
   */
  const exportThreadHtml = async (threadId, { fallbackName = DEFAULT_FILENAME } = {}) => {
    if (!threadId || exportingThreadIds.value.has(threadId)) return ''

    addExporting(threadId)
    const hide = message.loading('正在生成导出文件…', 0)
    try {
      // 方法挂在 threadApi（会话线程域）而非 agentApi，改调用对象前先看 apis/agent_api.js 的导出归属
      const response = await threadApi.exportThreadHtml(threadId)
      const filename = await saveBlobResponse(response, fallbackName)
      message.success(`已导出：${filename}`)
      return filename
    } catch (error) {
      console.warn('导出会话失败:', error)
      message.error(error?.message || '导出会话失败')
      return ''
    } finally {
      hide()
      removeExporting(threadId)
    }
  }

  return { isExporting, exportThreadHtml }
}
