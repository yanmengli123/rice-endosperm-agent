<template>
  <Teleport to="body">
    <button
      ref="ballRef"
      type="button"
      class="agent-floating-ball"
      :class="{ streaming: isStreaming, 'has-error': hasError, dragging: isDragging }"
      :style="ballStyle"
      :aria-label="isStreaming ? '快捷问答（回答生成中）' : '快捷问答'"
      :aria-expanded="panelOpen"
      @pointerdown="onBallPointerDown"
      @keydown.enter.prevent="togglePanel"
      @keydown.space.prevent="togglePanel"
    >
      <Sparkles :size="20" aria-hidden="true" />
      <span v-if="hasError" class="ball-error-dot" aria-hidden="true"></span>
    </button>

    <FloatingQuickChatPanel
      v-if="panelOpen"
      ref="panelRef"
      :items="items"
      :is-streaming="isStreaming"
      :is-sending="isSending"
      :status-text="statusText"
      :has-thread="hasThread"
      :agent-name="agentName"
      :agent-ready="agentReady"
      :is-mobile="isMobile"
      :prefers-reduced-motion="prefersReducedMotion"
      :panel-style="panelStyle"
      @send="chat.send"
      @stop="chat.stop"
      @retry="chat.retry"
      @new-thread="chat.startNewThread"
      @open-main="openInMainView"
      @close="closePanel"
    />
  </Teleport>
</template>

<script setup>
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Sparkles } from '@lucide/vue'
import FloatingQuickChatPanel from '@/components/floating/FloatingQuickChatPanel.vue'
import { useFloatingChat } from '@/composables/useFloatingChat'
import { useAgentStore } from '@/stores/agent'
import { useFloatingChatStore } from '@/stores/floatingChat'
import {
  BALL_SIZE,
  clampBallPosition,
  defaultBallPosition,
  isDragMovement,
  snapBallPosition
} from '@/utils/floatingBall'

/**
 * /agent 悬浮球快捷问答（挂载于 AgentView，仅 /agent 路由可见）：
 * - 球本体：pointer-capture 拖拽（对标 FloatingReaderWindow），位移 <4px 视为点击展开，
 *   松手 12px 吸边，位置经 pinia persistedstate 持久化；
 * - 问答引擎 useFloatingChat 常驻球内（面板只是视图），收起面板不中断流；
 * - keep-alive 失活断开订阅（run 在服务端继续），激活时经快照/active_run 续流。
 */
const router = useRouter()
const agentStore = useAgentStore()
const floatingStore = useFloatingChatStore()
const chat = useFloatingChat()
// 解构出响应式状态供模板直接使用（普通对象属性不会自动解包 ref）
const { items, isStreaming, isSending, statusText, threadId } = chat
const hasThread = computed(() => Boolean(threadId.value))

const ballRef = ref(null)
const panelRef = ref(null)
const panelOpen = ref(false)
const isDragging = ref(false)
// 拖拽过程中的临时位置；null 表示未在拖拽
const dragPosition = ref(null)

const viewport = ref({ width: 0, height: 0 })
const updateViewport = () => {
  viewport.value = { width: window.innerWidth, height: window.innerHeight }
}

const mediaQuery = window.matchMedia('(max-width: 640px)')
const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
const isMobile = ref(mediaQuery.matches)
const prefersReducedMotion = ref(reducedMotionQuery.matches)
const onMediaChange = (event) => {
  isMobile.value = event.matches
}
const onReducedMotionChange = (event) => {
  prefersReducedMotion.value = event.matches
}

// 展示位置：拖拽中用临时位，否则用持久化位（渲染前钳制，防旧值越界）
const displayPosition = computed(() => {
  if (dragPosition.value) return dragPosition.value
  return (
    clampBallPosition(floatingStore.ballPosition, viewport.value) ||
    defaultBallPosition(viewport.value)
  )
})

const ballStyle = computed(() => ({
  left: `${displayPosition.value.x}px`,
  top: `${displayPosition.value.y}px`,
  width: `${BALL_SIZE}px`,
  height: `${BALL_SIZE}px`
}))

// 桌面端面板优先在球的左上方展开；放不下时翻到右侧/下方。移动端由 CSS 底部抽屉接管。
const panelStyle = computed(() => {
  const pos = displayPosition.value
  const width = Math.min(380, viewport.value.width - 24)
  const height = Math.min(540, viewport.value.height - 24)
  let x = pos.x + BALL_SIZE - width
  if (x < 12) x = Math.min(pos.x + BALL_SIZE + 12, viewport.value.width - width - 12)
  let y = pos.y - height - 12
  if (y < 12) y = Math.min(pos.y + BALL_SIZE + 12, viewport.value.height - height - 12)
  return { left: `${Math.round(x)}px`, top: `${Math.round(y)}px`, width: `${width}px` }
})

const lastAssistantFailed = computed(() => {
  for (let i = items.value.length - 1; i >= 0; i -= 1) {
    if (items.value[i].role === 'assistant') return Boolean(items.value[i].error)
    if (items.value[i].role === 'user') return false
  }
  return false
})

const hasError = computed(() => lastAssistantFailed.value && !isStreaming.value)

const agentIdForName = computed(() => {
  if (threadId.value) {
    // 持久化线程绑定创建时的智能体
    return floatingStore.ballThreadAgentId || agentStore.selectedAgentId
  }
  return agentStore.selectedAgentId
})

const agentName = computed(() => {
  const agent = agentStore.agents.find((item) => item.id === agentIdForName.value)
  return agent?.name || '智能体'
})

const agentReady = computed(() => Boolean(agentIdForName.value) || hasThread.value)

// ==================== 拖拽 / 点击 ====================

const onBallPointerDown = (event) => {
  if (event.button !== 0) return
  const el = event.currentTarget
  el.setPointerCapture(event.pointerId)
  const start = { x: event.clientX, y: event.clientY }
  const origin = { ...displayPosition.value }
  let dragged = false

  const cleanup = () => {
    el.removeEventListener('pointermove', onMove)
    el.removeEventListener('pointerup', onUp)
    el.removeEventListener('pointercancel', onCancel)
    el.removeEventListener('lostpointercapture', cleanup)
    if (el.hasPointerCapture?.(event.pointerId)) el.releasePointerCapture(event.pointerId)
  }

  const onMove = (moveEvent) => {
    if (!dragged && isDragMovement(start, { x: moveEvent.clientX, y: moveEvent.clientY })) {
      dragged = true
      isDragging.value = true
    }
    if (dragged) {
      dragPosition.value =
        clampBallPosition(
          {
            x: origin.x + moveEvent.clientX - start.x,
            y: origin.y + moveEvent.clientY - start.y
          },
          viewport.value
        ) || dragPosition.value
    }
  }

  const finishDrag = () => {
    isDragging.value = false
    const snapped = snapBallPosition(dragPosition.value, viewport.value)
    floatingStore.setBallPosition(snapped || dragPosition.value)
    dragPosition.value = null
  }

  const onUp = () => {
    cleanup()
    if (dragged) {
      finishDrag()
    } else {
      togglePanel()
    }
  }

  const onCancel = () => {
    cleanup()
    if (dragged) finishDrag()
  }

  el.addEventListener('pointermove', onMove)
  el.addEventListener('pointerup', onUp)
  el.addEventListener('pointercancel', onCancel)
  el.addEventListener('lostpointercapture', cleanup)
}

// ==================== 面板开关 ====================

const togglePanel = () => {
  if (panelOpen.value) {
    closePanel()
    return
  }
  panelOpen.value = true
  // 面板刚 v-if 挂载，等 ref 就绪后聚焦输入框（focusInput 内部再过一拍 nextTick）
  setTimeout(() => panelRef.value?.focusInput?.(), 0)
}

const closePanel = () => {
  panelOpen.value = false
}

// 全局监听常驻（关闭态下均为 no-op），避免开合时反复绑定/解绑
const onDocumentPointerDown = (event) => {
  if (!panelOpen.value) return
  const target = event.target
  if (ballRef.value?.contains(target)) return
  if (panelRef.value?.$el?.contains(target)) return
  closePanel()
}

const onWindowKeydown = (event) => {
  if (event.key === 'Escape' && panelOpen.value) {
    event.stopPropagation()
    closePanel()
  }
}

const openInMainView = () => {
  if (!threadId.value) return
  void router.push({ name: 'AgentCompWithThreadId', params: { thread_id: threadId.value } })
}

// ==================== 生命周期 ====================

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
  mediaQuery.addEventListener('change', onMediaChange)
  reducedMotionQuery.addEventListener('change', onReducedMotionChange)
  document.addEventListener('pointerdown', onDocumentPointerDown, true)
  window.addEventListener('keydown', onWindowKeydown)
  void chat.resume()
})

onActivated(() => {
  // keep-alive 切回 /agent：续接历史与仍在跑的 run
  void chat.resume()
})

onDeactivated(() => {
  // 切走路由：断开订阅（run 在服务端继续），快照留在 localStorage 供回来续流
  chat.dispose()
})

onBeforeUnmount(() => {
  chat.dispose()
  window.removeEventListener('resize', updateViewport)
  mediaQuery.removeEventListener('change', onMediaChange)
  reducedMotionQuery.removeEventListener('change', onReducedMotionChange)
  document.removeEventListener('pointerdown', onDocumentPointerDown, true)
  window.removeEventListener('keydown', onWindowKeydown)
})
</script>

<style lang="less" scoped>
@ball-z-index: 980;

.agent-floating-ball {
  position: fixed;
  z-index: @ball-z-index;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--main-700);
  color: var(--gray-0);
  cursor: pointer;
  box-shadow: var(--shadow-4, 0 8px 24px rgba(0, 0, 0, 0.18));
  transition:
    background-color 0.15s ease,
    box-shadow 0.15s ease;
  touch-action: none;
  user-select: none;
}

.agent-floating-ball:hover {
  background: var(--main-800);
}

.agent-floating-ball:focus-visible {
  outline: 2px solid var(--main-500);
  outline-offset: 2px;
}

.agent-floating-ball.dragging {
  cursor: grabbing;
  transition: none;
}

.agent-floating-ball.streaming::after {
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 2px solid var(--main-500);
  animation: ball-pulse 1.6s ease-in-out infinite;
}

@keyframes ball-pulse {
  0%,
  100% {
    opacity: 0.25;
    transform: scale(1);
  }
  50% {
    opacity: 0.9;
    transform: scale(1.06);
  }
}

.ball-error-dot {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-error-500);
  border: 2px solid var(--gray-0);
}

@media (prefers-reduced-motion: reduce) {
  .agent-floating-ball.streaming::after {
    animation: none;
    opacity: 0.6;
    transform: none;
  }
}
</style>
