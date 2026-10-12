/**
 * 悬浮球定位纯函数。DOM 操作与事件监听在组件层（AgentFloatingBall.vue），
 * 这里只做可单测的坐标数学，职责划分对标 selectionPopup.js。
 */

export const BALL_SIZE = 48
export const BALL_VIEWPORT_MARGIN = 12
/** 位移达到该阈值判定为拖拽，否则视为点击 */
export const BALL_DRAG_THRESHOLD_PX = 4
/** 松手时距视口左右边缘小于该值则贴边（FloatingReaderWindow 同阈值） */
export const BALL_SNAP_PX = 12
/** 默认停靠右下角时上抬的偏移，避开主输入区右下角的模型选择/发送按钮 */
export const BALL_DEFAULT_BOTTOM_OFFSET = 180

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

const isFinitePoint = (point) =>
  Boolean(point) && Number.isFinite(point.x) && Number.isFinite(point.y)

const isFiniteViewport = (viewport) =>
  Boolean(viewport) && Number.isFinite(viewport.width) && Number.isFinite(viewport.height)

/**
 * 把球钳制回视口内（窗口缩小、持久化坐标越界时收敛）。
 * 视口过小时保持在左上边距处，不会跑出屏幕。
 * @returns {{x:number, y:number}|null} 入参非法时返回 null
 */
export function clampBallPosition(
  position,
  viewport,
  { size = BALL_SIZE, margin = BALL_VIEWPORT_MARGIN } = {}
) {
  if (!isFinitePoint(position) || !isFiniteViewport(viewport)) return null
  const maxX = Math.max(margin, viewport.width - size - margin)
  const maxY = Math.max(margin, viewport.height - size - margin)
  return {
    x: clamp(Math.round(position.x), margin, maxX),
    y: clamp(Math.round(position.y), margin, maxY)
  }
}

/** 视口右下角默认位（上抬避开输入区），入参非法时退到左上边距。 */
export function defaultBallPosition(
  viewport,
  { size = BALL_SIZE, margin = BALL_VIEWPORT_MARGIN } = {}
) {
  if (!isFiniteViewport(viewport)) return { x: BALL_VIEWPORT_MARGIN, y: BALL_VIEWPORT_MARGIN }
  const clamped = clampBallPosition(
    {
      x: viewport.width - size - margin - 12,
      y: viewport.height - size - margin - BALL_DEFAULT_BOTTOM_OFFSET
    },
    viewport,
    { size, margin }
  )
  return clamped || { x: margin, y: margin }
}

/**
 * 吸边：球靠近视口左/右边缘时贴边（取更近的一侧）。
 * @returns {{x:number, y:number, snapped:boolean}|null}
 */
export function snapBallPosition(
  position,
  viewport,
  { size = BALL_SIZE, margin = BALL_VIEWPORT_MARGIN, snapPx = BALL_SNAP_PX } = {}
) {
  const clamped = clampBallPosition(position, viewport, { size, margin })
  if (!clamped) return null
  const leftGap = clamped.x - margin
  const rightGap = viewport.width - margin - size - clamped.x
  if (leftGap <= snapPx && leftGap <= rightGap) {
    return { ...clamped, x: margin, snapped: true }
  }
  if (rightGap <= snapPx) {
    return { ...clamped, x: viewport.width - margin - size, snapped: true }
  }
  return { ...clamped, snapped: false }
}

/** 点击 vs 拖拽判定：指针位移是否达到拖拽阈值。 */
export function isDragMovement(
  startPoint,
  endPoint,
  { threshold = BALL_DRAG_THRESHOLD_PX } = {}
) {
  if (!isFinitePoint(startPoint) || !isFinitePoint(endPoint)) return false
  const dx = endPoint.x - startPoint.x
  const dy = endPoint.y - startPoint.y
  return Math.hypot(dx, dy) >= threshold
}
