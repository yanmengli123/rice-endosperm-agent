import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * 悬浮球快捷问答的持久化状态。
 *
 * 面板展开态刻意不持久化：每次进入 /agent 球可见、面板默认收起，
 * 避免一进页面就遮挡主会话。球位置与专属线程 id 跨会话保留。
 */
export const useFloatingChatStore = defineStore(
  'floatingChat',
  () => {
    // 球左上角坐标（px）；null 表示尚未拖过，使用默认右下位。渲染前需过 clampBallPosition。
    const ballPosition = ref(null)

    // 悬浮球专属会话：跨窗口持续复用，保留多轮上下文；空串表示下次提问时懒创建。
    const ballThreadId = ref('')
    // 线程创建时绑定的智能体（线程与智能体绑定，与主聊天同语义）
    const ballThreadAgentId = ref('')

    function setBallPosition(position) {
      ballPosition.value = position
    }

    function setBallThread(threadId, agentId) {
      ballThreadId.value = threadId || ''
      ballThreadAgentId.value = agentId || ''
    }

    function resetBallThread() {
      ballThreadId.value = ''
      ballThreadAgentId.value = ''
    }

    return {
      ballPosition,
      ballThreadId,
      ballThreadAgentId,
      setBallPosition,
      setBallThread,
      resetBallThread
    }
  },
  {
    persist: {
      key: 'floating-chat-store',
      storage: localStorage,
      pick: ['ballPosition', 'ballThreadId', 'ballThreadAgentId']
    }
  }
)
