/**
 * 轨迹投影降级遥测（审计缺口②）：所有静默兜底在此留痕。
 *
 * 企业级要求「降级可计数」：畸形快照/无效序列/纯环 span 等兜底路径
 * 不能无人可知。基线是 console.warn 带 run_id + reason（同一 (runId, reason)
 * 只告警一次防刷屏）；sink 可注入供测试捕获，未来如需接入服务端遥测
 * 通道，替换 sink 即可，调用点零改动。
 */

let activeSink = (payload) => {
  console.warn(`[trace-projection] ${payload.reason}`, payload)
}

const warnedKeys = new Set()

/** 注入遥测 sink（测试用）；传 null 恢复默认 console.warn。 */
export const setTraceDegradationSink = (sink) => {
  activeSink =
    sink ||
    ((payload) => {
      console.warn(`[trace-projection] ${payload.reason}`, payload)
    })
}

/** 清空去重表（测试隔离用）。 */
export const resetTraceDegradationTelemetry = () => {
  warnedKeys.clear()
}

/**
 * 上报一次投影降级。同一 (runId, reason) 只真正告警一次。
 * @returns {boolean} 本次是否真正送达 sink（false = 已去重）
 */
export const reportTraceDegradation = ({ runId = null, reason, detail = null }) => {
  const key = `${runId || '-'}|${reason}`
  if (warnedKeys.has(key)) return false
  warnedKeys.add(key)
  try {
    activeSink({ runId: runId || null, reason, detail })
  } catch {
    // sink 失败绝不影响投影主流程
  }
  return true
}
