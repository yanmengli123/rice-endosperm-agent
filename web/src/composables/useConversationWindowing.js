/**
 * 会话窗口化（P2 性能）：默认渲染最近 CONVERSATION_WINDOW_STEP 轮会话，
 * 顶部「显示更早」按步扩展并补偿滚动位置（视口停留在原消息处而不是跳顶）。
 * 流式新增恒定落在窗口尾部，不受影响；切换线程自动重置回默认窗口。
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  CONVERSATION_WINDOW_STEP,
  countHiddenConversations,
  windowConversationRows
} from '@/utils/conversationWindowing'

export function useConversationWindowing({ conversations, currentChatId, getScroller }) {
  const conversationWindow = ref(CONVERSATION_WINDOW_STEP)
  watch(currentChatId, () => {
    conversationWindow.value = CONVERSATION_WINDOW_STEP
  })

  const windowedConversationRows = (rows) => windowConversationRows(rows, conversationWindow.value)
  const hiddenConversationCount = computed(() =>
    countHiddenConversations(conversations.value?.length || 0, conversationWindow.value)
  )

  const showEarlierConversations = async () => {
    const scroller = getScroller?.()
    const prevHeight = scroller?.scrollHeight || 0
    conversationWindow.value += CONVERSATION_WINDOW_STEP
    await nextTick()
    // 头部插入后补偿滚动位置，视口停留在原消息处而不是跳顶
    if (scroller) {
      scroller.scrollTop += (scroller.scrollHeight || 0) - prevHeight
    }
  }

  return {
    conversationWindow,
    windowedConversationRows,
    hiddenConversationCount,
    showEarlierConversations
  }
}
