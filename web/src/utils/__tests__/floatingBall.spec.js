import assert from 'node:assert/strict'

import {
  BALL_SIZE,
  clampBallPosition,
  defaultBallPosition,
  isDragMovement,
  snapBallPosition
} from '../floatingBall.js'

const VIEWPORT = { width: 1280, height: 800 }

const run = () => {
  // clampBallPosition：合法坐标原样保留（取整）
  assert.deepEqual(clampBallPosition({ x: 300.6, y: 200.4 }, VIEWPORT), { x: 301, y: 200 })

  // 四个方向越界都收敛回边界
  assert.equal(clampBallPosition({ x: -50, y: 400 }, VIEWPORT).x, 12)
  assert.equal(clampBallPosition({ x: 9999, y: 400 }, VIEWPORT).x, 1280 - 48 - 12)
  assert.equal(clampBallPosition({ x: 300, y: -50 }, VIEWPORT).y, 12)
  assert.equal(clampBallPosition({ x: 300, y: 9999 }, VIEWPORT).y, 800 - 48 - 12)

  // 视口小于球+双倍边距时保持在左上边距，不出屏
  const tiny = clampBallPosition({ x: 500, y: 500 }, { width: 40, height: 40 })
  assert.deepEqual(tiny, { x: 12, y: 12 })

  // 非法入参返回 null（持久化值被污染时由调用方回退默认位）
  assert.equal(clampBallPosition(null, VIEWPORT), null)
  assert.equal(clampBallPosition({ x: 'a', y: 1 }, VIEWPORT), null)
  assert.equal(clampBallPosition({ x: 1, y: 1 }, null), null)

  // defaultBallPosition：右下角、上抬避开输入区
  const def = defaultBallPosition(VIEWPORT)
  assert.equal(def.x, 1280 - BALL_SIZE - 12 - 12)
  assert.equal(def.y, 800 - BALL_SIZE - 12 - 180)
  // 矮视口时上抬量被钳制，仍不出屏
  const shortDef = defaultBallPosition({ width: 1280, height: 200 })
  assert.ok(shortDef.y >= 12 && shortDef.y <= 200 - BALL_SIZE - 12)
  // 非法视口退到左上边距
  assert.deepEqual(defaultBallPosition(null), { x: 12, y: 12 })

  // snapBallPosition：左右贴边，取更近一侧
  assert.deepEqual(snapBallPosition({ x: 20, y: 300 }, VIEWPORT), {
    x: 12,
    y: 300,
    snapped: true
  })
  assert.equal(snapBallPosition({ x: 1280 - 48 - 18, y: 300 }, VIEWPORT).x, 1280 - 48 - 12)
  assert.equal(snapBallPosition({ x: 1280 - 48 - 18, y: 300 }, VIEWPORT).snapped, true)
  // 中间位置不吸附
  assert.equal(snapBallPosition({ x: 640, y: 300 }, VIEWPORT).snapped, false)
  // 越界入参先钳制再吸附
  assert.equal(snapBallPosition({ x: -999, y: 300 }, VIEWPORT).snapped, true)
  assert.equal(snapBallPosition(null, VIEWPORT), null)

  // isDragMovement：阈值判定
  assert.equal(isDragMovement({ x: 100, y: 100 }, { x: 102, y: 101 }), false)
  assert.equal(isDragMovement({ x: 100, y: 100 }, { x: 104, y: 102 }), true)
  assert.equal(isDragMovement({ x: 100, y: 100 }, null), false)
  assert.equal(isDragMovement(null, { x: 1, y: 1 }), false)
}

run()
console.log('floatingBall.spec.js 全部通过')
