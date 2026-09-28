/**
 * 协议能力快照（GET /api/agent/protocol）：模块级单例缓存，应用生命周期内
 * 只拉一次。能力位是 UI 渲染的显式开关（additive-only 语义）：
 * `hasCapability('trace_stage_facets')` 为 false 时消费方应显式降级
 * （隐藏阶段条并上报遥测），而不是靠数据字段缺席隐式兜底。
 *
 * 拉取失败按「无能力」处理并上报遥测——绝不因能力查询失败阻塞对话主链路。
 */
import { ref } from 'vue'
import { agentApi } from '@/apis'
import { reportTraceDegradation } from '@/utils/traceTelemetry'

const capabilities = ref(null)
let loadPromise = null

export const CAPABILITY_TRACE_STAGE_FACETS = 'trace_stage_facets'
export const CAPABILITY_FOLLOWUP_SUGGESTIONS = 'followup_suggestions'

export function useProtocolCapabilities() {
  const ensureCapabilities = () => {
    if (!loadPromise) {
      loadPromise = agentApi
        .getAgentProtocol()
        .then((snapshot) => {
          capabilities.value = Array.isArray(snapshot?.capabilities) ? snapshot.capabilities : []
        })
        .catch((error) => {
          capabilities.value = []
          reportTraceDegradation({
            runId: null,
            reason: 'protocol_capabilities_unavailable',
            detail: { message: error?.message || String(error) }
          })
        })
    }
    return loadPromise
  }

  const hasCapability = (key) =>
    Array.isArray(capabilities.value) && capabilities.value.includes(key)

  return { capabilities, ensureCapabilities, hasCapability }
}
