<template>
  <div
    class="float-window"
    :class="{ snapping: !docked && snapping, docked }"
    role="group"
    :aria-label="title || '浮动阅读窗'"
    :style="
      docked
        ? null
        : {
            left: rect.x + 'px',
            top: rect.y + 'px',
            width: rect.w + 'px',
            height: rect.h + 'px',
            zIndex
          }
    "
    @pointerdown="$emit('focus')"
  >
    <header v-if="!docked" class="float-title" @pointerdown="startDrag" @dblclick="$emit('dock')">
      <span class="float-title-text" :title="title">{{ title }}</span>
      <span class="float-title-actions">
        <button class="btn mini" title="回到并排布局（停靠）" @click.stop="$emit('dock')">
          停靠
        </button>
        <button
          class="btn mini"
          title="关闭"
          :aria-label="`关闭 ${title || '浮动阅读窗'}`"
          @click.stop="$emit('close')"
        >
          ✕
        </button>
      </span>
    </header>
    <div class="float-body">
      <slot></slot>
    </div>
    <template v-if="!docked"
      ><span
        v-for="dir in HANDLES"
        :key="dir"
        :class="['float-handle', `handle-${dir}`]"
        aria-hidden="true"
        @pointerdown="startResize($event, dir)"
      ></span
    ></template>
  </div>
</template>

<script setup>
import { reactive, ref, watch, onMounted, onBeforeUnmount } from 'vue'

/**
 * 浮动阅读窗（对标 PaperAgent 浮动多窗的 Web 形态）：
 * - 标题栏拖动、8 向缩放手柄、双击标题停靠回并排网格；
 * - 拖动/缩放时距视口边缘 ≤12px 自动吸附（PaperAgent ui.js 同阈值）；
 * - pointer capture 保证移出窗口仍跟手；视口允许时最小 320×240，整窗钳制回视口。
 */
const props = defineProps({
  title: { type: String, default: '' },
  initial: { type: Object, default: () => ({ x: 80, y: 90, w: 640, h: 520 }) },
  zIndex: { type: Number, default: 100 },
  docked: { type: Boolean, default: false }
})
defineEmits(['close', 'dock', 'focus'])

const HANDLES = ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']
const EDGE_SNAP_PX = 12
const MIN_W = 320
const MIN_H = 240

const rect = reactive({ ...props.initial })
const snapping = ref(false)
let releasePointer = () => {},
  snapTimer = null
const stopInteraction = () => {
  releasePointer()
  clearTimeout(snapTimer)
  snapping.value = false
}
watch(() => props.docked, stopInteraction)
const capture = (event, onMove) => {
  releasePointer()
  const target = event.currentTarget
  target.setPointerCapture(event.pointerId)
  const cleanup = () => {
    target.removeEventListener('pointermove', onMove)
    target.removeEventListener('pointerup', onUp)
    target.removeEventListener('pointercancel', cleanup)
    target.removeEventListener('lostpointercapture', cleanup)
    if (target.hasPointerCapture?.(event.pointerId)) target.releasePointerCapture(event.pointerId)
    releasePointer = () => {}
  }
  const onUp = () => {
    cleanup()
    applySnap()
  }
  target.addEventListener('pointermove', onMove)
  target.addEventListener('pointerup', onUp)
  target.addEventListener('pointercancel', cleanup)
  target.addEventListener('lostpointercapture', cleanup)
  releasePointer = cleanup
}

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

const applySnap = () => {
  let snapped = false
  if (Math.abs(rect.x) <= EDGE_SNAP_PX) {
    rect.x = 0
    snapped = true
  }
  if (Math.abs(rect.y) <= EDGE_SNAP_PX) {
    rect.y = 0
    snapped = true
  }
  const right = rect.x + rect.w
  const bottom = rect.y + rect.h
  if (Math.abs(right - window.innerWidth) <= EDGE_SNAP_PX) {
    rect.w = window.innerWidth - rect.x
    snapped = true
  }
  if (Math.abs(bottom - window.innerHeight) <= EDGE_SNAP_PX) {
    rect.h = window.innerHeight - rect.y
    snapped = true
  }
  if (snapped) {
    snapping.value = true
    clearTimeout(snapTimer)
    snapTimer = setTimeout(() => (snapping.value = false), 180)
  }
}

const clampToViewport = () => {
  const width = Math.max(1, window.innerWidth)
  const height = Math.max(1, window.innerHeight)
  rect.w = clamp(rect.w, Math.min(MIN_W, width), width)
  rect.h = clamp(rect.h, Math.min(MIN_H, height), height)
  rect.x = clamp(rect.x, 0, width - rect.w)
  rect.y = clamp(rect.y, 0, height - rect.h)
}
onMounted(() => {
  clampToViewport()
  window.addEventListener('resize', clampToViewport)
})
onBeforeUnmount(() => {
  stopInteraction()
  window.removeEventListener('resize', clampToViewport)
})

const startDrag = (event) => {
  if (props.docked || event.button !== 0 || event.target.closest('button')) return
  const startX = event.clientX
  const startY = event.clientY
  const originX = rect.x
  const originY = rect.y
  const onMove = (move) => {
    rect.x = originX + move.clientX - startX
    rect.y = originY + move.clientY - startY
    clampToViewport()
  }
  capture(event, onMove)
}

const startResize = (event, dir) => {
  if (props.docked || event.button !== 0) return
  event.stopPropagation()
  const startX = event.clientX
  const startY = event.clientY
  const origin = { ...rect }
  const onMove = (move) => {
    const dx = move.clientX - startX
    const dy = move.clientY - startY
    if (dir.includes('e')) rect.w = origin.w + dx
    if (dir.includes('s')) rect.h = origin.h + dy
    if (dir.includes('w')) {
      rect.x = origin.x + dx
      rect.w = origin.w - dx
    }
    if (dir.includes('n')) {
      rect.y = origin.y + dy
      rect.h = origin.h - dy
    }
    if (rect.w < MIN_W) {
      if (dir.includes('w')) rect.x = origin.x + origin.w - MIN_W
      rect.w = MIN_W
    }
    if (rect.h < MIN_H) {
      if (dir.includes('n')) rect.y = origin.y + origin.h - MIN_H
      rect.h = MIN_H
    }
    clampToViewport()
  }
  capture(event, onMove)
}
</script>

<style lang="less" scoped>
.float-window {
  position: fixed;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--gray-150, #d9d9d9);
  border-radius: 10px;
  background: var(--color-bg-elevated, #fff);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
  overflow: hidden;
}
.float-window.snapping {
  outline: 2px solid var(--main-color, #4a7dbe);
  outline-offset: -2px;
}
.float-window.docked {
  position: relative;
  min-width: 0;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  overflow: visible;
  .float-body {
    overflow: visible;
  }
}
.float-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  font-size: 13px;
  font-weight: 600;
  cursor: grab;
  user-select: none;
  border-bottom: 1px solid var(--gray-150, #e8e8e8);
  background: var(--color-bg-container, #f5f5f2);
}
.float-title:active {
  cursor: grabbing;
}
.float-title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.float-title-actions {
  display: inline-flex;
  flex-shrink: 0;
  gap: 4px;
}
.btn.mini {
  padding: 1px 7px;
  font-size: 11px;
}
.float-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.float-handle {
  position: absolute;
  z-index: 2;
}
.handle-n,
.handle-s {
  left: 10px;
  right: 10px;
  height: 6px;
  cursor: ns-resize;
}
.handle-n {
  top: -3px;
}
.handle-s {
  bottom: -3px;
}
.handle-e,
.handle-w {
  top: 10px;
  bottom: 10px;
  width: 6px;
  cursor: ew-resize;
}
.handle-e {
  right: -3px;
}
.handle-w {
  left: -3px;
}
.handle-ne,
.handle-nw,
.handle-se,
.handle-sw {
  width: 12px;
  height: 12px;
}
.handle-ne {
  top: -4px;
  right: -4px;
  cursor: nesw-resize;
}
.handle-sw {
  bottom: -4px;
  left: -4px;
  cursor: nesw-resize;
}
.handle-nw {
  top: -4px;
  left: -4px;
  cursor: nwse-resize;
}
.handle-se {
  bottom: -4px;
  right: -4px;
  cursor: nwse-resize;
}
</style>
