<template>
  <section v-if="user.isAdmin" class="pdf-operations" aria-label="PDF 翻译运行诊断">
    <h4>PDF 翻译运行诊断</h4>
    <button class="btn" :disabled="busy" @click="load">刷新诊断</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="snapshot">
      <p>
        排队 {{ snapshot.status_counts.PENDING || 0 }} · 执行中
        {{ snapshot.status_counts.RUNNING || 0 }} · 用量待核对 {{ snapshot.unknown_calls }} ·
        清理积压 {{ snapshot.cleanup_overdue }}
      </p>
      <p>
        最近 24 小时：{{ snapshot.terminal_samples }} 个终态任务；失败率
        {{
          snapshot.failure_rate === null
            ? '暂无数据'
            : `${(snapshot.failure_rate * 100).toFixed(1)}%`
        }}。
      </p>
      <p class="muted" v-if="snapshot.slo_evidence === 'insufficient_samples'">
        样本不足 20 个，尚不足以评估稳定性目标。
      </p>
      <ul v-if="snapshot.alerts.length">
        <li v-for="alert in snapshot.alerts" :key="alert.code">{{ alert.message }}</li>
      </ul>
      <p v-else>当前检查未发现运行告警。</p>
    </template>
    <div v-for="row in unresolved" :key="row.id" class="usage-row">
      <p>
        任务 {{ row.job_id }} · 请求 {{ row.id }} · 未知用量预留 {{ row.reserved_tokens }} tokens
      </p>
      <button class="btn" @click="selectReceipt(row)">核对供应商回执</button>
    </div>
    <form v-if="receiptRow" @submit.prevent="submitReceipt">
      <p>请根据供应商账单或调用回执填写。核对后会按实际用量调整额度，记录提交人并保留原账本。</p>
      <label
        >输入 tokens<input
          v-model.number="receipt.input_tokens"
          type="number"
          min="0"
          max="10000000"
          required
      /></label>
      <label
        >输出 tokens<input
          v-model.number="receipt.output_tokens"
          type="number"
          min="0"
          max="10000000"
          required
      /></label>
      <label
        >供应商回执编号<input v-model="receipt.receipt_ref" minlength="8" maxlength="256" required
      /></label>
      <label
        >核对说明<textarea v-model="receipt.reason" minlength="8" maxlength="2048" required />
      </label>
      <p>
        本次确认实际用量：{{ (receipt.input_tokens ?? 0) + (receipt.output_tokens ?? 0) }} tokens；原预留
        {{ receiptRow.reserved_tokens }} tokens。
      </p>
      <button class="btn" :disabled="submitting" type="submit">提交核对结果</button>
      <button class="btn" type="button" @click="receiptRow = null">取消填写</button>
    </form>
  </section>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { literaturePdfApi } from '@/apis/literature_api'
import { useUserStore } from '@/stores/user'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'

const user = useUserStore(),
  polling = useLiteraturePollingScope(
    () => {
      ++generation
      controller?.abort()
      busy.value = false
    },
    () => load()
  )
const snapshot = ref(null),
  unresolved = ref([]),
  receiptRow = ref(null),
  receipt = ref({})
const busy = ref(false),
  submitting = ref(false),
  error = ref('')
let generation = 0,
  controller = null
const owner = () => JSON.stringify([user.uid, user.departmentId, user.userRole])
const load = async () => {
  if (!user.isAdmin || !polling.value) return
  controller?.abort()
  const request = (controller = new AbortController()),
    ticket = ++generation,
    identity = owner()
  busy.value = true
  try {
    const [report, usage] = await Promise.all([
      literaturePdfApi.operations(request.signal),
      literaturePdfApi.unresolvedUsage(request.signal)
    ])
    if (ticket !== generation || owner() !== identity || request.signal.aborted) return
    snapshot.value = report
    unresolved.value = usage.items
    error.value = ''
  } catch (err) {
    if (ticket === generation && !request.signal.aborted)
      error.value = err.message || '诊断读取失败'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const selectReceipt = (row) => {
  receiptRow.value = { ...row }
  receipt.value = { input_tokens: null, output_tokens: null, receipt_ref: '', reason: '' }
}
const submitReceipt = async () => {
  if (submitting.value || !receiptRow.value || !user.isAdmin) return
  const identity = owner(),
    row = { ...receiptRow.value },
    payload = { ...receipt.value, expected_digest: row.request_digest }
  submitting.value = true
  try {
    await literaturePdfApi.resolveUsage(row.id, payload)
    if (owner() !== identity) return
    receiptRow.value = null
    await load()
  } catch (err) {
    if (owner() === identity) error.value = err.message || '回执核对失败'
  } finally {
    submitting.value = false
  }
}
watch([() => user.uid, () => user.departmentId, () => user.userRole], () => {
  ++generation
  controller?.abort()
  snapshot.value = null
  unresolved.value = []
  receiptRow.value = null
  load()
})
onMounted(load)
onBeforeUnmount(() => {
  ++generation
  controller?.abort()
})
</script>

<style scoped lang="less">
.pdf-operations {
  margin: 20px 0;
  padding: 14px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
}
.muted {
  color: var(--color-text-secondary);
}
.usage-row {
  border-top: 1px solid var(--gray-200);
  padding: 8px 0;
}
form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 10px 0;
}
</style>
