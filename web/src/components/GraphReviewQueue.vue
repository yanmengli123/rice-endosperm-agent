<template>
  <a-drawer
    :open="open"
    title="审核队列"
    width="860"
    destroy-on-close
    @close="$emit('update:open', false)"
  >
    <div class="queue-header">
      <a-radio-group v-model:value="status" size="small" @change="reload">
        <a-radio-button value="CANDIDATE">待审核 {{ counts.CANDIDATE || 0 }}</a-radio-button>
        <a-radio-button value="APPROVED">已验证 {{ counts.APPROVED || 0 }}</a-radio-button>
        <a-radio-button value="REJECTED">已拒绝 {{ counts.REJECTED || 0 }}</a-radio-button>
        <a-radio-button value="CANONICAL">规范层 {{ counts.CANONICAL || 0 }}</a-radio-button>
      </a-radio-group>
      <a-select v-model:value="order" size="small" style="width: 150px" @change="reload">
        <a-select-option value="support_asc">佐证少优先</a-select-option>
        <a-select-option value="support_desc">佐证多优先</a-select-option>
        <a-select-option value="recent">最近更新</a-select-option>
      </a-select>
    </div>

    <div v-if="status === 'CANONICAL'" class="queue-hint">
      规范层（托管导入）对象只读，不接受人工审核决策。
    </div>
    <div v-else-if="status === 'REJECTED'" class="queue-hint">
      已拒绝的关系保留审计痕迹，不再出现在图上；再次抽出同一条时重放钩子会自动保持拒绝。
    </div>

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :pagination="pagination"
      row-key="triple_id"
      size="small"
      :row-selection="
        status === 'CANDIDATE'
          ? { selectedRowKeys, onChange: (keys) => (selectedRowKeys = keys) }
          : undefined
      "
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'relation'">
          <div class="relation-cell">
            <span class="entity-name">{{ record.source?.name || record.source_entity_id }}</span>
            <span class="relation-arrow">—{{ relationLabel(record.relation_type) }}→</span>
            <span class="entity-name">{{ record.target?.name || record.target_entity_id }}</span>
          </div>
          <div class="relation-types">
            <a-tag size="small">{{ record.source?.label }}</a-tag>
            <a-tag size="small">{{ record.target?.label }}</a-tag>
          </div>
        </template>
        <template v-else-if="column.key === 'quote'">
          <div class="quote-preview" :title="record.preview_quote">
            {{ record.preview_quote || '（无引文）' }}
          </div>
          <div class="quote-meta">
            <span>{{ record.mention_count }} 处 · {{ record.file_count }} 文件</span>
            <a-tag v-if="record.hedge_any" size="small" color="gold">推测性</a-tag>
            <a-tag v-if="record.trigger_verified_any" size="small" color="green">触发词</a-tag>
            <a-tag v-if="record.verifier_confirmed_any" size="small" color="green">复核</a-tag>
            <a-tag v-if="record.pinned_any" size="small" color="blue">已固定证据</a-tag>
          </div>
        </template>
        <template v-else-if="column.key === 'actions'">
          <template v-if="status === 'CANDIDATE'">
            <a-button
              size="small"
              type="primary"
              :loading="actingId === record.triple_id"
              @click="approveRow(record)"
            >
              验证
            </a-button>
            <a-button size="small" danger class="queue-action-btn" @click="openReject(record)"
              >拒绝</a-button
            >
          </template>
          <template v-else-if="status === 'APPROVED'">
            <a-button size="small" @click="openReject(record, true)">撤销验证</a-button>
          </template>
          <template v-else>
            <span class="queue-hint-inline">—</span>
          </template>
        </template>
      </template>
    </a-table>

    <div v-if="selectedRowKeys.length && status === 'CANDIDATE'" class="batch-bar">
      <span>已选 {{ selectedRowKeys.length }} 条</span>
      <a-button type="primary" size="small" :loading="batchLoading" @click="batchApprove"
        >批量验证</a-button
      >
      <a-button danger size="small" :loading="batchLoading" @click="openReject(null)"
        >批量拒绝</a-button
      >
      <a-button size="small" @click="selectedRowKeys = []">清除选择</a-button>
    </div>

    <a-modal
      v-model:open="rejectModalOpen"
      :title="rejectTitle"
      :confirm-loading="batchLoading"
      ok-text="确认拒绝"
      ok-type="danger"
      @ok="confirmReject"
    >
      <p class="reject-target" v-if="rejectTarget">
        {{ rejectTarget.source?.name }} —{{ relationLabel(rejectTarget.relation_type) }}→
        {{ rejectTarget.target?.name }}
      </p>
      <a-textarea
        v-model:value="rejectReason"
        :rows="3"
        placeholder="拒绝理由（必填，审计可查）：如「方向反了，句子说的是 B 抑制 A」"
      />
    </a-modal>
  </a-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'

const props = defineProps({
  open: Boolean,
  kbId: String
})
const emit = defineEmits(['update:open', 'reviewed'])

const status = ref('CANDIDATE')
const order = ref('support_asc')
const page = ref(1)
const pageSize = ref(20)
const items = ref([])
const total = ref(0)
const counts = ref({})
const loading = ref(false)
const selectedRowKeys = ref([])
const actingId = ref(null)
const batchLoading = ref(false)
const rejectModalOpen = ref(false)
const rejectReason = ref('')
const rejectTarget = ref(null)
const revokeMode = ref(false)

const columns = [
  { key: 'relation', title: '关系', width: 300 },
  { key: 'quote', title: '引文预览与佐证' },
  { key: 'actions', title: '操作', width: 180 }
]

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  showTotal: (t) => `共 ${t} 条`
}))

const rejectTitle = computed(() =>
  revokeMode.value
    ? '撤销验证（填写理由）'
    : selectedRowKeys.value.length && !rejectTarget.value
      ? '批量拒绝'
      : '拒绝关系'
)

const relationLabel = (type) => type || ''

const load = async () => {
  if (!props.kbId) return
  loading.value = true
  try {
    const res = await graphApi.reviewQueue({
      kb_id: props.kbId,
      status: status.value,
      page: page.value,
      page_size: pageSize.value,
      order: order.value
    })
    items.value = res?.data?.items || []
    total.value = res?.data?.total || 0
    counts.value = res?.data?.counts?.triples || {}
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '队列加载失败')
  } finally {
    loading.value = false
  }
}

const reload = () => {
  page.value = 1
  selectedRowKeys.value = []
  load()
}

const onTableChange = (pager) => {
  page.value = pager.current
  pageSize.value = pager.pageSize
  load()
}

const approveRow = async (record) => {
  actingId.value = record.triple_id
  try {
    const res = await graphApi.reviewApprove({
      kb_id: props.kbId,
      target_kind: 'TRIPLE',
      target_id: record.triple_id,
      if_version: record.review_version || undefined
    })
    if (res?.data?.unchanged) {
      message.info('该关系已是验证状态（幂等，未重复记录）')
    } else {
      message.success('已验证：决策与 pinned 证据已记录，重抽/重建后自动恢复')
    }
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '验证失败')
  } finally {
    actingId.value = null
  }
}

const openReject = (record, revoke = false) => {
  rejectTarget.value = record
  revokeMode.value = Boolean(revoke)
  rejectReason.value = ''
  rejectModalOpen.value = true
}

const confirmReject = async () => {
  if (!rejectReason.value.trim()) {
    message.warning('拒绝必须填写理由')
    return
  }
  batchLoading.value = true
  try {
    if (rejectTarget.value) {
      await graphApi.reviewReject({
        kb_id: props.kbId,
        target_kind: 'TRIPLE',
        target_id: rejectTarget.value.triple_id,
        reason: rejectReason.value,
        if_version: rejectTarget.value.review_version || undefined
      })
      message.success(revokeMode.value ? '已撤销验证' : '已拒绝：图上投影与向量已清理，审计可查')
    } else {
      const res = await graphApi.reviewBatch({
        kb_id: props.kbId,
        action: 'REJECT',
        reason: rejectReason.value,
        targets: selectedRowKeys.value.map((id) => ({ kind: 'TRIPLE', id }))
      })
      const skipped = res?.data?.skipped?.length || 0
      message.success(
        `批量拒绝完成：成功 ${res?.data?.succeeded?.length || 0} 条${skipped ? `，跳过 ${skipped} 条` : ''}`
      )
    }
    rejectModalOpen.value = false
    selectedRowKeys.value = []
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '拒绝失败')
  } finally {
    batchLoading.value = false
  }
}

const batchApprove = async () => {
  batchLoading.value = true
  try {
    const res = await graphApi.reviewBatch({
      kb_id: props.kbId,
      action: 'APPROVE',
      targets: selectedRowKeys.value.map((id) => ({ kind: 'TRIPLE', id }))
    })
    const skipped = res?.data?.skipped?.length || 0
    message.success(
      `批量验证完成：成功 ${res?.data?.succeeded?.length || 0} 条${skipped ? `，跳过 ${skipped} 条（版本冲突或无效）` : ''}`
    )
    selectedRowKeys.value = []
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '批量验证失败')
  } finally {
    batchLoading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      reload()
    }
  },
  { immediate: true }
)
</script>

<style scoped lang="less">
.queue-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.queue-hint {
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 10px;
}

.queue-hint-inline {
  color: var(--gray-400);
}

.relation-cell {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  font-size: 13px;

  .entity-name {
    font-weight: 600;
    color: var(--gray-900);
  }

  .relation-arrow {
    color: var(--main-700);
    font-size: 12px;
  }
}

.relation-types {
  margin-top: 2px;
}

.quote-preview {
  font-size: 12px;
  color: var(--gray-700);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.quote-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--gray-500);
}

.queue-action-btn {
  margin-left: 6px;
}

.batch-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  background: var(--color-trans-light, #fff);
  border-top: 1px solid var(--gray-200);
  font-size: 13px;
}

.reject-target {
  font-size: 13px;
  color: var(--gray-800);
  margin-bottom: 8px;
}
</style>
