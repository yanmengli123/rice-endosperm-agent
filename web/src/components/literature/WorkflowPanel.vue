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
    if (!disposed && ['PENDING', 'RUNNING'].includes(state.value.status))
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
onUnmounted(() => {
  disposed = true
  stop()
})
</script>

<style scoped>
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
