import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useChatUIStore = defineStore(
  'chatUI',
  () => {
    // ==================== 聊天界面 UI 状态 ====================
    // 加载状态
    const isLoadingMessages = ref(false)

    // 应用侧边栏折叠态
    const sidebarCollapsed = ref(false)

    // 用户手动选择的聊天模型。作为应用级偏好持久化，避免路由或线程切换时回退到默认模型。
    const selectedModelSpec = ref('')

    // 更多菜单
    const moreMenuOpen = ref(false)
    const moreMenuPosition = ref({ x: 0, y: 0 })

    // ==================== 方法 ====================
    /**
     * 打开更多菜单
     * @param {number} x - X 坐标
     * @param {number} y - Y 坐标
     */
    function openMoreMenu(x, y) {
      moreMenuPosition.value = { x, y }
      moreMenuOpen.value = true
    }

    /**
     * 关闭更多菜单
     */
    function closeMoreMenu() {
      moreMenuOpen.value = false
    }

    /**
     * 更新应用级聊天模型偏好
     * @param {string} modelSpec - provider:model 格式的模型标识；空字符串表示使用智能体默认模型
     */
    function setSelectedModelSpec(modelSpec) {
      if (typeof modelSpec !== 'string') return
      selectedModelSpec.value = modelSpec.trim()
    }

    /**
     * 重置所有 UI 状态（不包括持久化状态）
     */
    function reset() {
      isLoadingMessages.value = false
      moreMenuOpen.value = false
      moreMenuPosition.value = { x: 0, y: 0 }
    }

    return {
      // 状态
      isLoadingMessages,
      sidebarCollapsed,
      selectedModelSpec,
      moreMenuOpen,
      moreMenuPosition,

      // 方法
      openMoreMenu,
      closeMoreMenu,
      setSelectedModelSpec,
      reset
    }
  },
  {
    persist: {
      key: 'chat-ui-store',
      storage: localStorage,
      pick: ['sidebarCollapsed', 'selectedModelSpec']
    }
  }
)
