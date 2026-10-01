import { onBeforeUnmount, readonly, ref } from 'vue'

/**
 * 通用列宽拖拽（Pointer Events）。
 *
 * 与 AgentPanel 内置的 resize-handle 同一套交互约定，抽成 composable 供
 * AppLayout 侧边栏等更多分栏复用：
 * - setPointerCapture 保证指针滑过 iframe / 子元素时 move 事件不中断；
 * - move 回调经 requestAnimationFrame 节流，避免高频重排；
 * - 拖拽期间锁定 body 光标与文本选择，pointercancel / 组件卸载时统一恢复；
 * - 宽度语义是相对增量（onResize 回传 clientX 与起点 startClientX），且指针
 *   未移动超过 moveThreshold 时不提交——纯点击命中区不会改动宽度。
 *
 * @param {Object} options
 * @param {(clientX: number, startClientX: number) => void} options.onResize 拖拽过程中（已节流）的回调
 * @param {() => void} [options.onStart] 拖拽开始时回调
 * @param {() => void} [options.onEnd] 拖拽结束时回调（pointercancel 与组件卸载也会触发）
 * @param {number} [options.moveThreshold=3] 判定「确实在拖拽」的最小位移（px）
 * @returns {{ isDragging: import('vue').Readonly<import('vue').Ref<boolean>>, startDrag: (event: PointerEvent) => void, stopDrag: (event?: PointerEvent) => void }}
 */
export function useColumnResize({ onResize, onStart, onEnd, moveThreshold = 3 }) {
  const isDragging = ref(false)
  let resizePointerId = null
  let startClientX = 0
  let pendingClientX = 0
  let hasMoved = false
  let resizeFrameId = 0

  const flushResize = () => {
    resizeFrameId = 0
    if (!isDragging.value || !hasMoved) return
    onResize(pendingClientX, startClientX)
  }

  const onPointerMove = (event) => {
    if (!isDragging.value || event.pointerId !== resizePointerId) return
    pendingClientX = event.clientX
    if (Math.abs(pendingClientX - startClientX) > moveThreshold) {
      hasMoved = true
    }
    if (hasMoved && !resizeFrameId) {
      resizeFrameId = window.requestAnimationFrame(flushResize)
    }
  }

  const stopDrag = (event) => {
    if (!isDragging.value || (event && event.pointerId !== resizePointerId)) return

    if (resizeFrameId) {
      window.cancelAnimationFrame(resizeFrameId)
      resizeFrameId = 0
    }

    if (event && hasMoved) {
      pendingClientX = event.clientX
      onResize(pendingClientX, startClientX)
    }

    isDragging.value = false
    resizePointerId = null
    hasMoved = false
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', stopDrag)
    window.removeEventListener('pointercancel', stopDrag)
    onEnd?.()
  }

  const startDrag = (event) => {
    if (event.button !== 0 || isDragging.value) return

    isDragging.value = true
    resizePointerId = event.pointerId
    startClientX = event.clientX
    pendingClientX = event.clientX
    hasMoved = false
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    try {
      event.currentTarget?.setPointerCapture?.(event.pointerId)
    } catch {
      // 非受信/已释放指针调用 setPointerCapture 会抛 InvalidPointerId；
      // 捕获失败只影响越过子元素的事件路由，window 级监听仍然完整
    }
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', stopDrag)
    window.addEventListener('pointercancel', stopDrag)
    onStart?.()
  }

  onBeforeUnmount(() => stopDrag())

  return { isDragging: readonly(isDragging), startDrag, stopDrag }
}
