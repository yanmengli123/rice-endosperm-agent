<template>
  <section>
    <p>复用知识源 PDF 契约的现有解析版本；阅读派生物不会成为原文证据。</p>
    <select v-model="mode" :disabled="busy">
      <option value="parse_compile">阅读渲染 + 编译 + 导航发布</option>
      <option value="full">完整流程（复核、编译、翻译、摘要、发布）</option>
      <option value="parse_only">仅生成阅读视图（已有来源解析）</option>
    </select>
    <button class="btn" :disabled="busy" @click="start">{{ busy ? '提交中…' : '启动流程' }}</button>
    <button class="btn" v-if="runId" @click="refresh">刷新状态</button>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state">
      <p>运行：{{ state.run_id }} · {{ state.status }}</p>
      <ol>
        <li v-for="step in state.steps" :key="step.step">
          {{ step.step }}：{{ step.status }} <span v-if="step.detail"> — {{ step.detail }}</span>
        </li>
      </ol>
      <div v-if="awaitingReview">
        <p>请在“识别复核”中完成裁决；完成后后台自动推进。</p>
        <input v-model="skipReason" placeholder="显式跳过复核的原因（必填）" />
        <button class="btn" :disabled="!skipReason.trim() || busy" @click="skipReview">记录原因并跳过</button>
      </div>
      <div v-if="cancellable" class="cancel-block">
        <input v-model="cancelReason" placeholder="取消原因（可选）" />
        <button class="btn danger" :disabled="busy" @click="cancel">取消本次运行</button>
      </div>
      <p v-else-if="state.cancel_requested" class="cancel-pending">
        已请求取消，等待当前步骤收尾（不回滚已生成的产物与已计费用量）。
      </p>
      <p v-if="state.status === 'CANCELLED'" class="cancel-pending">
        运行已取消。已完成步骤的产物保留，可在下方继续阅读或重新启动。
      </p>
    </template>
  </section>
</template>

<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
const props = defineProps({ rid: { type: String, required: true } })
const mode = ref('parse_compile')
const busy = ref(false)
const runId = ref('')
const state = ref(null)
const notice = ref('')
const skipReason = ref('')
const cancelReason = ref('')
// 取消是显式动作且不可回滚，用 confirm 兜住误点。
const cancellable = computed(
  () => Boolean(state.value?.cancellable) && !state.value?.cancel_requested
)
let timer = null
let disposed = false
const awaitingReview = computed(
  () =>
    state.value?.status === 'RUNNING' &&
    state.value.steps?.some(
      (step) =>
        step.step === 'review_gate' && step.status === 'PENDING' && step.detail?.startsWith('等待')
    )
)
const stop = () => {
  if (timer) clearTimeout(timer)
  timer = null
}
const refresh = async () => {
  stop()
  if (disposed || !runId.value) return
  try {
    state.value = await apiGet(`/api/literature/workflow/${encodeURIComponent(runId.value)}`)
    // 必须包含 CANCEL_REQUESTED：取消后仍需轮询到 CANCELLED，
    // 否则用户点了取消就永远看不到收敛结果。
    if (!disposed && ['PENDING', 'RUNNING', 'CANCEL_REQUESTED'].includes(state.value.status))
      timer = setTimeout(refresh, 3000)
  } catch (err) {
    notice.value = `状态读取失败：${err?.message || err}`
  }
}
const start = async () => {
  busy.value = true
  try {
    const result = await apiPost(
      `/api/literature/papers/${encodeURIComponent(props.rid)}/workflow`,
      { mode: mode.value }
    )
    runId.value = result.run_id
    notice.value = result.enqueue_deferred
      ? '运行已持久化，等待后台补偿。'
      : '流程已提交；入队不代表处理完成。'
    await refresh()
  } catch (err) {
    notice.value = `启动失败：${err?.message || err}`
  } finally {
    busy.value = false
  }
}
const skipReview = async () => {
  busy.value = true
  try {
    await apiPost(`/api/literature/workflow/${encodeURIComponent(runId.value)}/skip-review`, {
      reason: skipReason.value.trim()
    })
    await refresh()
  } catch (err) {
    notice.value = `跳过失败：${err?.message || err}`
  } finally {
    busy.value = false
  }
}
const cancel = async () => {
  if (!window.confirm('取消后当前步骤会中断，且已生成的产物与已计费用量不会回滚。确认取消？'))
    return
  busy.value = true
  try {
    const result = await apiPost(`/api/literature/workflow/${encodeURIComponent(runId.value)}/cancel`, {
      reason: cancelReason.value.trim()
    })
    notice.value = result.already_requested
      ? '取消已在处理中，等待当前步骤收尾。'
      : '已请求取消，等待当前步骤收尾。'
    await refresh()
  } catch (err) {
    notice.value = `取消失败：${err?.message || err}`
  } finally {
    busy.value = false
  }
}
onUnmounted(() => {
  disposed = true
  stop()
})
</script>

<style scoped>
.cancel-block {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 8px;
}
.btn.danger {
  color: var(--el-color-danger, #f56c6c);
  border-color: var(--el-color-danger, #f56c6c);
  background: transparent;
}
.cancel-pending {
  color: var(--el-color-warning, #e6a23c);
}
section {
  line-height: 1.7;
  color: var(--color-text);
}
select,
input,
button {
  margin: 4px;
  padding: 6px;
}
</style>
