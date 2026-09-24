<template>
  <component
    :is="embedded ? 'div' : 'a-drawer'"
    v-bind="
      embedded
        ? { class: 'queue-embedded' }
        : { open, title: '门禁送审队列', width: '860', destroyOnClose: true }
    "
    v-on="embedded ? {} : { close: () => $emit('update:open', false) }"
  >
    <div class="queue-header">
      <a-radio-group v-model:value="status" size="small" @change="reload">
        <a-radio-button value="PENDING">待裁决 {{ counts.PENDING?._total || 0 }}</a-radio-button>
        <a-radio-button value="RESOLVED">已裁决 {{ counts.RESOLVED?._total || 0 }}</a-radio-button>
        <a-radio-button value="ALL">全部</a-radio-button>
      </a-radio-group>
      <a-select
        v-model:value="gateCode"
        size="small"
        style="width: 220px"
        allow-clear
        placeholder="按门禁过滤"
        @change="reload"
      >
        <a-select-option value="G9_NEGATION_REVIEW">G9 否定矛盾</a-select-option>
        <a-select-option value="G7_TRIGGER_UNVERIFIED">G7 触发词未过</a-select-option>
      </a-select>
    </div>

    <div class="queue-hint">
      门禁 REVIEW 路由的候选：PROMOTE 升格为人工批准三元组（引文逐字复核）， DISCARD
      关闭。裁决幂等，审计可查。
    </div>

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :pagination="pagination"
      row-key="review_id"
      size="small"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'candidate'">
          <div class="relation-cell">
            <span class="entity-name">{{ record.candidate?.subject }}</span>
            <span class="relation-arrow">—{{ record.candidate?.predicate }}→</span>
            <span class="entity-name">{{ record.candidate?.object }}</span>
          </div>
          <div class="quote">
            「{{ record.candidate?.evidence_quote }}」
            <a-tag color="orange" size="small">{{ record.gate_code }}</a-tag>
          </div>
        </template>
        <template v-else-if="column.key === 'source'">
          <div class="file-name">{{ record.filename }}</div>
          <div class="chunk-id">{{ record.chunk_id }}</div>
        </template>
        <template v-else-if="column.key === 'context'">
          <div class="chunk-context">{{ contextPreview(record) }}</div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag v-if="record.status === 'PENDING'" color="orange">待裁决</a-tag>
          <a-tag v-else color="green">{{ record.resolution || '已裁决' }}</a-tag>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space v-if="record.status === 'PENDING'">
            <a-button
              size="small"
              type="primary"
              :loading="actingId === record.review_id"
              @click="resolve(record, 'PROMOTE')"
            >
              采纳
            </a-button>
            <a-button
              size="small"
              danger
              :loading="actingId === record.review_id"
              @click="resolve(record, 'DISCARD')"
            >
              丢弃
            </a-button>
          </a-space>
          <span v-else>{{ record.resolved_by }}</span>
        </template>
      </template>
    </a-table>
  </component>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'

const props = defineProps({
  open: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  embedded: { type: Boolean, default: false }
})
const emit = defineEmits(['update:open', 'reviewed'])

const status = ref('PENDING')
const gateCode = ref(null)
const items = ref([])
const counts = ref({})
const loading = ref(false)
const actingId = ref(null)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

const columns = [
  { title: '候选关系', key: 'candidate', width: 300 },
  { title: '来源', key: 'source', width: 160 },
  { title: '原文上下文', key: 'context' },
  { title: '状态', key: 'status', width: 130 },
  { title: '操作', key: 'actions', width: 140 }
]

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: false,
  showTotal: (t) => `共 ${t} 条`
}))

const contextPreview = (record) => {
  const quote = record.candidate?.evidence_quote || ''
  const content = record.chunk_content || ''
  const index = content.indexOf(quote)
  if (index < 0) return content.slice(0, 120)
  const start = Math.max(0, index - 40)
  return (start > 0 ? '…' : '') + content.slice(start, index + quote.length + 40)
}

const load = async () => {
  loading.value = true
  try {
    const res = await graphApi.gateReviewQueue({
      kb_id: props.kbId,
      status: status.value,
      gate_code: gateCode.value || undefined,
      page: page.value,
      page_size: pageSize.value
    })
    items.value = res?.data?.items || []
    counts.value = res?.data?.counts || {}
    total.value = res?.data?.total || 0
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '加载门禁送审队列失败')
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

const resolve = async (record, action) => {
  actingId.value = record.review_id
  try {
    const res = await graphApi.gateReviewResolve({
      kb_id: props.kbId,
      review_id: record.review_id,
      action
    })
    if (res?.data?.unchanged) {
      message.info('该项已被裁决（幂等，未重复记录）')
    } else if (action === 'PROMOTE') {
      message.success('已升格为人工批准三元组（决策 + 审计 + 投影同步）')
    } else {
      message.success('已丢弃，审计可查')
    }
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '裁决失败')
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
.relation-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.entity-name {
  font-weight: 600;
}
.relation-arrow {
  color: #1677ff;
  font-family: monospace;
  font-size: 12px;
}
.quote {
  margin-top: 4px;
  color: #555;
  font-size: 12px;
}
.file-name {
  font-size: 12px;
}
.chunk-id {
  color: #999;
  font-size: 11px;
  font-family: monospace;
}
.chunk-context {
  color: #666;
  font-size: 12px;
}
</style>
