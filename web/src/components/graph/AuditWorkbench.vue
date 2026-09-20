<template>
  <div class="audit-workbench">
    <div class="audit-toolbar">
      <a-input
        v-model:value="filters.actor_uid"
        placeholder="按操作者 uid 过滤"
        size="small"
        style="width: 200px"
        allow-clear
        @press-enter="reload"
      />
      <a-select
        v-model:value="filters.action"
        size="small"
        style="width: 200px"
        allow-clear
        placeholder="按动作过滤"
        :options="actionOptions"
        @change="reload"
      />
      <a-input-search
        v-model:value="filters.target_id"
        placeholder="按目标 ID 过滤"
        size="small"
        style="width: 260px"
        @search="reload"
      />
      <a-button size="small" :loading="loading" @click="reload">查询</a-button>
      <a-button size="small" @click="exportCsv">导出 CSV</a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :pagination="pagination"
      row-key="id"
      size="small"
      :custom-row="auditCustomRow"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-tag :color="actionColor(record.action)" size="small">{{ auditActionLabel(record.action) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'target'">
          <span class="mono" :title="record.target_id">{{ record.target_id?.slice(0, 12) }}…</span>
          <a-tag size="small">{{ record.target_kind }}</a-tag>
        </template>
        <template v-else-if="column.key === 'diff'">
          <a-button v-if="hasSnapshot(record)" size="small" type="link" @click.stop="openDiff(record)">
            前后差异
          </a-button>
          <span v-else class="audit-hint">—</span>
        </template>
      </template>
    </a-table>

    <a-modal v-model:open="diffOpen" title="决策前后差异" width="680px" ok-text="关闭" :footer="null">
      <template v-if="diffRecord">
        <div class="diff-meta">
          {{ formatTime(diffRecord.created_at) }} · {{ diffRecord.actor_uid }} ·
          {{ auditActionLabel(diffRecord.action) }}
          <template v-if="diffRecord.reason"> — {{ diffRecord.reason }}</template>
        </div>
        <div class="diff-grid">
          <div class="diff-panel diff-panel--before">
            <div class="diff-title">变更前</div>
            <pre>{{ formatSnapshot(diffRecord.before_snapshot) }}</pre>
          </div>
          <div class="diff-panel diff-panel--after">
            <div class="diff-title">变更后</div>
            <pre>{{ formatSnapshot(diffRecord.after_snapshot) }}</pre>
          </div>
        </div>
      </template>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'
import { auditActionLabel } from '@/utils/graph/reviewMeta'

const props = defineProps({
  kbId: { type: String, required: true }
})

const filters = reactive({ actor_uid: null, action: null, target_id: null })
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const diffOpen = ref(false)
const diffRecord = ref(null)

const columns = [
  { title: '时间', dataIndex: 'created_at', key: 'time', width: 170 },
  { title: '操作者', dataIndex: 'actor_uid', key: 'actor', width: 130 },
  { title: '动作', key: 'action', width: 150 },
  { title: '目标', key: 'target', width: 220 },
  { title: '理由 / 备注', dataIndex: 'reason', key: 'reason' },
  { title: '差异', key: 'diff', width: 100 }
]

const actionOptions = [
  'APPROVE',
  'REJECT',
  'SUPERSEDE',
  'ADD_RELATION',
  'REEXTRACT_CHUNK',
  'GATE_PROMOTE',
  'GATE_DISCARD',
  'CONFLICT_RESOLVE',
  'GOVERNANCE_SETTINGS_UPDATE'
].map((value) => ({ value, label: auditActionLabel(value) }))

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: true,
  pageSizeOptions: ['20', '50', '100'],
  showTotal: (t) => `共 ${t} 条`
}))

const actionColor = (action) =>
  ({
    APPROVE: 'green',
    REJECT: 'red',
    SUPERSEDE: 'orange',
    GOVERNANCE_SETTINGS_UPDATE: 'purple',
    GATE_PROMOTE: 'cyan',
    CONFLICT_RESOLVE: 'orange'
  })[action] || 'default'

const hasSnapshot = (record) =>
  Boolean(record.before_snapshot || record.after_snapshot)

const formatSnapshot = (snapshot) =>
  snapshot ? JSON.stringify(snapshot, null, 2) : '（无）'

const formatTime = (iso) => (iso ? new Date(iso).toLocaleString() : '—')

const load = async () => {
  if (!props.kbId) return
  loading.value = true
  try {
    const res = await graphApi.reviewAudit({
      kb_id: props.kbId,
      page: page.value,
      page_size: pageSize.value,
      actor_uid: filters.actor_uid || undefined,
      action: filters.action || undefined,
      target_id: filters.target_id || undefined
    })
    items.value = res?.data?.items || []
    total.value = res?.data?.total || 0
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '审计账本加载失败')
  } finally {
    loading.value = false
  }
}

const reload = () => {
  page.value = 1
  load()
}

const onTableChange = (pager) => {
  page.value = pager.current
  pageSize.value = pager.pageSize
  load()
}

const auditCustomRow = (record) => ({
  onClick: () => {
    if (hasSnapshot(record)) openDiff(record)
  }
})

const openDiff = (record) => {
  diffRecord.value = record
  diffOpen.value = true
}

const exportCsv = async () => {
  try {
    const response = await graphApi.reviewAuditExport(props.kbId, {
      actor_uid: filters.actor_uid || undefined,
      action: filters.action || undefined,
      target_id: filters.target_id || undefined
    })
    const blob = await response.blob()
    const link = document.createElement('a')
    link.href = window.URL.createObjectURL(blob)
    link.download = `graph-audit-${props.kbId}.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(link.href)
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '审计导出失败')
  }
}

watch(
  () => props.kbId,
  () => {
    filters.actor_uid = null
    filters.action = null
    filters.target_id = null
    reload()
  },
  { immediate: true }
)
</script>

<style scoped lang="less">
.audit-workbench {
  height: 100%;
  overflow-y: auto;
  padding: 12px;
}

.audit-toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.audit-hint {
  color: var(--gray-400);
}

.diff-meta {
  font-size: 13px;
  color: var(--gray-700);
  margin-bottom: 10px;
}

.diff-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.diff-panel {
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  overflow: hidden;

  .diff-title {
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    background: var(--gray-50);
  }

  pre {
    margin: 0;
    padding: 8px 10px;
    font-size: 11px;
    line-height: 1.5;
    max-height: 320px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }

  &--before .diff-title {
    color: var(--color-error, #cf1322);
  }

  &--after .diff-title {
    color: var(--color-success, #389e0d);
  }
}

.mono {
  font-family: monospace;
}
</style>
