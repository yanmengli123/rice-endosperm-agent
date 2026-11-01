<template>
  <section class="app-usage">
    <div class="heading"><h3>APP 接入与使用统计</h3><a-space>
      <a-select v-model:value="days" :options="[7, 30, 90, 365].map(value => ({ value, label: `近 ${value} 天` }))" @change="refresh" />
      <a-button :loading="loading" @click="refresh">刷新</a-button>
    </a-space></div>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <a-spin :spinning="loading">
      <p>{{ totals.app_count || 0 }} 个 APP · {{ totals.active_key_count || 0 }} 个有效凭据 · {{ totals.end_user_count || 0 }} 个终端用户 · {{ totals.input_count || 0 }} 次累计输入</p>
      <p>累计输入按已接收提交计数，幂等重试不重复计数。执行段和 Token 按所选时间统计，含估算时保留标记。</p>
      <a-table :columns="columns" :data-source="rows" row-key="app_id" size="small" :pagination="false" :scroll="{ x: 650 }">
        <template #bodyCell="{ column, record }"><template v-if="column.key === 'total_tokens'">{{ record.total_tokens }}{{ record.estimated_runs ? '（含估算）' : '' }}</template></template>
      </a-table>
    </a-spin>
    <details><summary>Public API 接入示例</summary>
      <p>使用该 APP 新建的密钥，设置稳定的终端用户标识；每次新任务使用新的 Idempotency-Key，同一任务重试沿用原值。</p>
      <a-select v-model:value="selected" :options="apps.map(app => ({ value: app.id, label: app.name }))" placeholder="选择 APP" style="min-width: 200px" />
      <pre>{{ example }}</pre><a-button @click="copy">复制示例</a-button>
      <p>会话事件：GET /api/v1/agents/sessions/{session_id}/events；重连携带 Last-Event-ID。密钥仅用于已授权资源。</p>
    </details>
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { useUserStore } from '@/stores/user'
import { publicAppApi } from '@/apis/public_app_api'
const props = defineProps({ apps: { type: Array, default: () => [] } })
const user = useUserStore()
const rows = ref([]), totals = ref({}), days = ref(30), loading = ref(false), error = ref(''), selected = ref('')
let request
const columns = [{ title: 'APP', dataIndex: 'name' }, { title: '累计输入', dataIndex: 'input_count' }, { title: '会话', dataIndex: 'session_count' }, { title: '执行段', dataIndex: 'run_count' }, { title: 'Token', key: 'total_tokens' }]
const example = computed(() => {
  const app = props.apps.find(item => item.id === selected.value) || props.apps[0]
  const body = JSON.stringify({ agent_id: app?.agent_slugs?.[0] || 'YOUR_AUTHORIZED_AGENT', input: '你好' })
  return `curl --request POST '${window.location.origin}/api/v1/agents/sessions' \\\n  --header 'Authorization: Bearer YOUR_APP_KEY' \\\n  --header 'X-End-User-Id: customer-001' \\\n  --header 'Idempotency-Key: example-request-001' \\\n  --header 'Content-Type: application/json' \\\n  --data '${body}'`
})
async function refresh() {
  request?.abort()
  const current = new AbortController(); request = current
  loading.value = true; error.value = ''
  try {
    const result = await publicAppApi.usage(days.value, { signal: current.signal })
    if (request === current) { rows.value = result.apps; totals.value = result.totals }
  } catch (e) { if (!current.signal.aborted && request === current) error.value = e.message || '使用统计加载失败' }
  finally { if (request === current) loading.value = false }
}
async function copy() { try { await navigator.clipboard.writeText(example.value); message.success('示例已复制') } catch { message.error('复制失败，请选择示例文本手动复制') } }
watch(() => user.uid, () => { rows.value = []; totals.value = {}; if (user.uid) refresh(); else request?.abort() }, { immediate: true })
onBeforeUnmount(() => request?.abort())
defineExpose({ refresh })
</script>
<style scoped lang="less">
.app-usage { margin: 20px 0; padding-bottom: 20px; border-bottom: 1px solid var(--gray-150); p { color: var(--color-text-secondary); font-size: 12px; } }
.heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
details { margin-top: 20px; summary { cursor: pointer; } pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--gray-50); border-radius: 8px; } }
</style>
