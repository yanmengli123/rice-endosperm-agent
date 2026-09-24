<template>
  <component
    :is="embedded ? 'div' : 'a-drawer'"
    v-bind="
      embedded
        ? { class: 'queue-embedded' }
        : { open, title: '冲突队列', width: '860', destroyOnClose: true }
    "
    v-on="embedded ? {} : { close: () => $emit('update:open', false) }"
  >
    <div class="queue-header">
      <a-radio-group v-model:value="status" size="small" @change="reload">
        <a-radio-button value="OPEN">待处置 {{ counts.OPEN?._total || 0 }}</a-radio-button>
        <a-radio-button value="RESOLVED">已处置 {{ counts.RESOLVED?._total || 0 }}</a-radio-button>
        <a-radio-button value="ALL">全部</a-radio-button>
      </a-radio-group>
      <a-select
        v-model:value="kind"
        size="small"
        style="width: 200px"
        allow-clear
        placeholder="按类型过滤"
        @change="reload"
      >
        <a-select-option value="DIRECTION">极性矛盾（DIRECTION）</a-select-option>
        <a-select-option value="DEFINITION">定义口径不一（DEFINITION）</a-select-option>
      </a-select>
    </div>

    <div class="queue-hint">
      冲突只登记不删证。处置三态：新代旧（SUPERSEDED）/ 并陈（CONTESTED）/
      条件互补（RECONCILED）——结论写进冲突行与审计，证据行保持原样。
    </div>

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :pagination="pagination"
      row-key="conflict_id"
      size="small"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'subject'">
          <a-tag :color="record.kind === 'DEFINITION' ? 'purple' : 'red'">{{ record.kind }}</a-tag>
          <span class="subject-ref">{{ subjectLabel(record) }}</span>
        </template>
        <template v-else-if="column.key === 'detail'">
          <div v-if="record.kind === 'DEFINITION'" class="detail-block">
            <div
              v-for="(interval, index) in definitionIntervals(record)"
              :key="index"
              class="interval-line"
            >
              <span class="interval">{{ interval.label }}</span>
              <span class="file">{{ interval.file }}</span>
            </div>
          </div>
          <div v-else class="detail-block">
            <div class="interval-line">
              <a-tag color="green" size="small">正</a-tag>
              <span class="quote">{{ polarityQuote(record, 'positive') }}</span>
            </div>
            <div class="interval-line">
              <a-tag color="red" size="small">负</a-tag>
              <span class="quote">{{ polarityQuote(record, 'negative') }}</span>
            </div>
            <div v-if="conditionLabel(record)" class="condition">
              条件：{{ conditionLabel(record) }}
            </div>
          </div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag v-if="record.status === 'OPEN'" color="red">待处置</a-tag>
          <a-tag v-else color="green">{{ record.resolution || '已处置' }}</a-tag>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-select
            v-if="record.status === 'OPEN'"
            :value="resolutions[record.conflict_id] || 'CONTESTED'"
            size="small"
            style="width: 130px"
            @change="(value) => (resolutions[record.conflict_id] = value)"
          >
            <a-select-option value="SUPERSEDED">新代旧</a-select-option>
            <a-select-option value="CONTESTED">并陈</a-select-option>
            <a-select-option value="RECONCILED">条件互补</a-select-option>
          </a-select>
          <a-button
            v-if="record.status === 'OPEN'"
            size="small"
            type="primary"
            style="margin-left: 8px"
            :loading="actingId === record.conflict_id"
            @click="resolve(record)"
          >
            处置
          </a-button>
          <span v-else>{{ record.resolved_by }}</span>
        </template>
      </template>
    </a-table>
  </component>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'

const props = defineProps({
  open: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  embedded: { type: Boolean, default: false }
})
const emit = defineEmits(['update:open', 'reviewed'])

const status = ref('OPEN')
const kind = ref(null)
const items = ref([])
const counts = ref({})
const loading = ref(false)
const actingId = ref(null)
const resolutions = reactive({})
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

const columns = [
  { title: '类型 / 主体', key: 'subject', width: 220 },
  { title: '冲突明细', key: 'detail' },
  { title: '状态', key: 'status', width: 110 },
  { title: '处置', key: 'actions', width: 240 }
]

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: false,
  showTotal: (t) => `共 ${t} 条`
}))

const subjectLabel = (record) =>
  record.kind === 'DEFINITION'
    ? record.detail?.entity || record.subject_ref
    : record.detail?.content || record.subject_ref

const definitionIntervals = (record) =>
  (record.detail?.definitions || []).map((definition) => ({
    label: `${definition.start}–${definition.end} ${definition.unit || ''}`,
    file: definition.file_id
  }))

const polarityQuote = (record, polarity) => {
  const items = record.detail?.[polarity] || []
  return items.map((item) => `「${(item.quote || '').slice(0, 80)}」`).join(' / ') || '—'
}

const conditionLabel = (record) => {
  const key = record.detail?.condition_key
  return key && key !== '_' ? key : ''
}

const load = async () => {
  loading.value = true
  try {
    const res = await graphApi.conflictQueue({
      kb_id: props.kbId,
      status: status.value,
      kind: kind.value || undefined,
      page: page.value,
      page_size: pageSize.value
    })
    items.value = res?.data?.items || []
    counts.value = res?.data?.counts || {}
    total.value = res?.data?.total || 0
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '加载冲突队列失败')
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

const resolve = async (record) => {
  actingId.value = record.conflict_id
  try {
    const res = await graphApi.conflictResolve({
      kb_id: props.kbId,
      conflict_id: record.conflict_id,
      resolution: resolutions[record.conflict_id] || 'CONTESTED'
    })
    if (res?.data?.unchanged) {
      message.info('该冲突已处置（幂等，未重复记录）')
    } else {
      message.success('冲突已处置，结论入账（证据行未动）')
    }
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '处置失败')
  } finally {
    actingId.value = null
  }
}

watch(
  () => props.open,
  (value) => {
    if (value) reload()
  }
)

watch(
  () => props.kbId,
  () => {
    if (props.embedded || props.open) reload()
  }
)
</script>

<style scoped>
.queue-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.queue-hint {
  color: #888;
  font-size: 12px;
  margin-bottom: 12px;
}
.subject-ref {
  font-weight: 600;
}
.detail-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.interval-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.interval {
  font-family: monospace;
  font-weight: 600;
}
.file {
  color: #999;
  font-size: 11px;
}
.quote {
  color: #555;
  font-size: 12px;
}
.condition {
  color: #722ed1;
  font-size: 12px;
}
</style>
