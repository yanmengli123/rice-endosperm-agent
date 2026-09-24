<template>
  <component
    :is="embedded ? 'div' : 'a-drawer'"
    v-bind="
      embedded
        ? { class: 'queue-embedded' }
        : { open, title: '审核队列', width: '860', destroyOnClose: true }
    "
    v-on="embedded ? {} : { close: () => $emit('update:open', false) }"
  >
    <div class="queue-header">
      <a-radio-group v-model:value="status" size="small" @change="reload">
        <a-radio-button value="CANDIDATE">待审核 {{ counts.CANDIDATE || 0 }}</a-radio-button>
        <a-radio-button value="APPROVED">人工批准 {{ counts.APPROVED || 0 }}</a-radio-button>
        <a-radio-button value="REJECTED">人工拒绝 {{ counts.REJECTED || 0 }}</a-radio-button>
        <a-radio-button value="CANONICAL">规范层 {{ counts.CANONICAL || 0 }}</a-radio-button>
      </a-radio-group>
      <a-select v-model:value="order" size="small" style="width: 160px" @change="reload">
        <a-select-option value="risk_desc">风险高优先</a-select-option>
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
      :custom-row="queueCustomRow"
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
            <a-tag v-if="record.conflict_status === 'CONTESTED'" size="small" color="red"
              >冲突中</a-tag
            >
            <a-tag
              v-if="record.task"
              size="small"
              color="blue"
              :title="`已被 ${record.task.assignee_uid} 领取`"
            >
              {{ record.task.assignee_uid }} 处理中
            </a-tag>
          </div>
        </template>
        <template v-else-if="column.key === 'quote'">
          <div class="quote-preview" :title="record.preview_quote">
            {{ record.preview_quote || '（无引文）' }}
          </div>
          <div class="quote-meta">
            <span>{{ record.mention_count }} 处 · {{ record.file_count }} 文件</span>
            <span
              v-if="record.risk_score != null"
              class="risk-score"
              title="机器证据风险分（越高越该先看）"
            >
              风险 {{ record.risk_score }}
            </span>
            <a-tag v-if="record.hedge_any" size="small" color="gold">推测性</a-tag>
            <a-tag v-if="record.trigger_verified_any" size="small" color="cyan"
              >机器校验·触发词</a-tag
            >
            <a-tag v-if="record.verifier_confirmed_any" size="small" color="cyan"
              >机器校验·复核</a-tag
            >
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
              批准
            </a-button>
            <a-button size="small" danger class="queue-action-btn" @click="openReject(record)"
              >拒绝</a-button
            >
          </template>
          <template v-else-if="status === 'APPROVED'">
            <a-button size="small" @click="openReject(record, true)">撤销批准</a-button>
          </template>
          <template v-else>
            <span class="queue-hint-inline">—</span>
          </template>
        </template>
      </template>
    </a-table>

    <div v-if="selectedRowKeys.length && status === 'CANDIDATE'" class="batch-bar">
      <span>已选 {{ selectedRowKeys.length }} 条</span>
      <a-button type="primary" size="small" :loading="batchLoading" @click="batchPreview"
        >批量批准（先预检）</a-button
      >
      <a-button danger size="small" :loading="batchLoading" @click="openReject(null)"
        >批量拒绝</a-button
      >
      <a-button size="small" @click="selectedRowKeys = []">清除选择</a-button>
    </div>

    <!-- 拒绝（单条/批量）：原因代码 + 必填说明，审计可查 -->
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
      <a-form layout="vertical">
        <a-form-item label="原因代码（结构化，便于统计检索）">
          <a-select
            v-model:value="rejectReasonCode"
            :options="reasonCodeOptions"
            allow-clear
            placeholder="选择原因代码（可选）"
          />
        </a-form-item>
        <a-form-item label="说明（必填）">
          <a-textarea
            v-model:value="rejectReason"
            :rows="3"
            placeholder="拒绝理由（必填，审计可查）：如「方向反了，句子说的是 B 抑制 A」"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 批量批准预检：准入档位、逐条判定、抽样预览；确认后逐条携带 pinned 证据 -->
    <a-modal
      v-model:open="previewModalOpen"
      title="批量批准预检"
      width="720px"
      :confirm-loading="batchLoading"
      ok-text="批准准入的条目"
      @ok="confirmBatchApprove"
    >
      <a-alert type="info" show-icon style="margin-bottom: 10px">
        <template #message>
          准入档位：{{ admissionLevelLabel }}（治理设置可配）。仅准入条目会被批准，每条将固定其最优
          OK 引文作为决策证据。
        </template>
      </a-alert>
      <div class="preview-summary">
        <a-tag color="green">准入 {{ batchPreviewSummary.admissible.length }} 条</a-tag>
        <a-tag v-if="batchPreviewSummary.blocked.length" color="orange">
          阻断 {{ batchPreviewSummary.blocked.length }} 条（需逐条人工）
        </a-tag>
      </div>
      <div class="preview-block-title">抽样预览（前 5 条准入项）</div>
      <ul class="preview-sample">
        <li v-for="sample in batchPreviewSummary.sample" :key="sample.id">
          <span class="quote-preview">{{ sample.quote }}</span>
          <span class="sample-chunk">{{ sample.chunkId }}</span>
        </li>
      </ul>
      <template v-if="batchPreviewSummary.blocked.length">
        <div class="preview-block-title">阻断原因分布</div>
        <div class="blocked-reasons">
          <a-tag
            v-for="(count, reason) in blockedReasonCounts"
            :key="reason"
            size="small"
            color="orange"
          >
            {{ previewReasonLabel(reason) }} × {{ count }}
          </a-tag>
        </div>
      </template>
    </a-modal>

    <!-- 批量执行回执：成功/跳过逐条明细（版本冲突与无效项分开呈现） -->
    <a-modal v-model:open="resultModalOpen" title="批量执行回执" width="720px" ok-text="知道了">
      <a-tabs size="small">
        <a-tab-pane :tab="`成功 ${batchResult.succeeded.length}`" key="ok">
          <div class="result-list">
            <div
              v-for="item in batchResult.succeeded"
              :key="item.id"
              class="result-row result-row--ok"
            >
              {{ item.id.slice(0, 12) }}… · v{{ item.version }} · 证据
              {{ item.pinned_chunk_id || '（未固定）' }}
            </div>
          </div>
        </a-tab-pane>
        <a-tab-pane :tab="`跳过 ${batchResult.skipped.length}`" key="skipped">
          <div v-if="!batchResult.skipped.length" class="queue-hint">没有跳过的条目。</div>
          <div class="result-list">
            <div
              v-for="item in batchResult.skipped"
              :key="item.id"
              class="result-row result-row--skipped"
            >
              <a-tag size="small" :color="item.reason === 'version_conflict' ? 'orange' : 'red'">
                {{ item.reason === 'version_conflict' ? '版本冲突（他人已更新）' : '无效条目' }}
              </a-tag>
              {{ item.id.slice(0, 12) }}… · {{ item.detail }}
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-modal>
  </component>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'
import {
  BATCH_ADMISSION_META,
  PREVIEW_REASONS,
  REASON_CODES,
  composeReason,
  summarizeBatchPreview
} from '@/utils/graph/reviewMeta'

const props = defineProps({
  open: Boolean,
  kbId: String,
  // 工作台模式：内嵌渲染（非 Drawer），供审核工作台三栏布局使用
  embedded: {
    type: Boolean,
    default: false
  }
})
const emit = defineEmits(['update:open', 'reviewed', 'select'])

// 工作台三栏联动：点击行在右栏展开证据与裁决面板
const queueCustomRow = (record) => ({
  onClick: () => emit('select', record)
})

const status = ref('CANDIDATE')
const order = ref('risk_desc')
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
const rejectReasonCode = ref(null)
const rejectTarget = ref(null)
const revokeMode = ref(false)
const previewModalOpen = ref(false)
const batchPreviewData = ref(null)
const resultModalOpen = ref(false)
const batchResult = ref({ succeeded: [], skipped: [] })

const reasonCodeOptions = REASON_CODES.map((item) => ({ value: item.value, label: item.label }))

const columns = [
  { key: 'relation', title: '关系', width: 300 },
  { key: 'quote', title: '引文预览与机器校验' },
  { key: 'actions', title: '人工决策', width: 180 }
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
    ? '撤销批准（填写理由）'
    : selectedRowKeys.value.length && !rejectTarget.value
      ? '批量拒绝'
      : '人工拒绝关系'
)

const batchPreviewSummary = computed(() => summarizeBatchPreview(batchPreviewData.value))
const admissionLevelLabel = computed(
  () => BATCH_ADMISSION_META[batchPreviewData.value?.admission_level]?.label || ''
)
const blockedReasonCounts = computed(() => {
  const countsByReason = {}
  for (const item of batchPreviewSummary.value.blocked) {
    countsByReason[item.reason] = (countsByReason[item.reason] || 0) + 1
  }
  return countsByReason
})
const previewReasonLabel = (reason) => PREVIEW_REASONS[reason] || reason

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
      message.info('该关系已是人工批准状态（幂等，未重复记录）')
    } else {
      message.success(
        `已人工批准：固定证据 ${res?.data?.decision?.pinned_chunk_id || '（自动选择）'}，重抽/重建后自动恢复`
      )
    }
    emit('reviewed')
    load()
  } catch (e) {
    if (e?.response?.status === 409) {
      message.warning('该关系已被其他审核人更新，已重新加载队列')
      load()
    } else {
      message.error(e?.response?.data?.detail || e?.message || '批准失败')
    }
  } finally {
    actingId.value = null
  }
}

const openReject = (record, revoke = false) => {
  rejectTarget.value = record
  revokeMode.value = Boolean(revoke)
  rejectReason.value = ''
  rejectReasonCode.value = null
  rejectModalOpen.value = true
}

const confirmReject = async () => {
  if (!rejectReason.value.trim()) {
    message.warning('拒绝必须填写理由')
    return
  }
  batchLoading.value = true
  try {
    const reason = composeReason(rejectReasonCode.value, rejectReason.value)
    if (rejectTarget.value) {
      await graphApi.reviewReject({
        kb_id: props.kbId,
        target_kind: 'TRIPLE',
        target_id: rejectTarget.value.triple_id,
        reason,
        if_version: rejectTarget.value.review_version || undefined
      })
      message.success(
        revokeMode.value ? '已撤销批准' : '已人工拒绝：图上投影与向量已清理，审计可查'
      )
    } else {
      const res = await graphApi.reviewBatch({
        kb_id: props.kbId,
        action: 'REJECT',
        reason,
        targets: selectedRowKeys.value.map((id) => ({ kind: 'TRIPLE', id }))
      })
      batchResult.value = {
        succeeded: res?.data?.succeeded || [],
        skipped: res?.data?.skipped || []
      }
      resultModalOpen.value = true
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

const batchPreview = async () => {
  batchLoading.value = true
  try {
    const res = await graphApi.governanceBatchPreview({
      kb_id: props.kbId,
      targets: selectedRowKeys.value.map((id) => ({ kind: 'TRIPLE', id }))
    })
    batchPreviewData.value = res?.data || null
    previewModalOpen.value = true
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '批量预检失败')
  } finally {
    batchLoading.value = false
  }
}

const confirmBatchApprove = async () => {
  const admissible = batchPreviewSummary.value.admissible
  if (!admissible.length) {
    message.warning('没有准入条目；请逐条人工处理')
    previewModalOpen.value = false
    return
  }
  batchLoading.value = true
  try {
    const res = await graphApi.reviewBatch({
      kb_id: props.kbId,
      action: 'APPROVE',
      targets: admissible.map((item) => ({
        kind: 'TRIPLE',
        id: item.target_id,
        pinned_chunk_id: item.pinned_chunk_id || undefined
      }))
    })
    batchResult.value = {
      succeeded: res?.data?.succeeded || [],
      skipped: res?.data?.skipped || []
    }
    previewModalOpen.value = false
    resultModalOpen.value = true
    selectedRowKeys.value = []
    emit('reviewed')
    load()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '批量批准失败')
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

watch(
  () => props.kbId,
  () => {
    if (props.embedded || props.open) {
      reload()
    }
  }
)

defineExpose({ reload, load })
</script>

<style scoped lang="less">
.queue-embedded {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.queue-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
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

  .risk-score {
    color: var(--color-warning-600, #d97706);
    font-weight: 600;
  }
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

.preview-summary {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.preview-block-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--gray-700);
  margin: 10px 0 6px;
}

.preview-sample {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--gray-700);

  li {
    margin-bottom: 4px;
  }

  .sample-chunk {
    margin-left: 8px;
    color: var(--gray-400);
    font-size: 11px;
  }
}

.blocked-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.result-list {
  max-height: 320px;
  overflow-y: auto;
  font-size: 12px;
}

.result-row {
  padding: 4px 0;
  border-bottom: 1px dashed var(--gray-100);
  color: var(--gray-700);

  &--ok {
    color: var(--gray-800);
  }
}
</style>
