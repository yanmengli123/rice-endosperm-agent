<template>
  <Teleport to="body">
    <div v-if="visible" class="workbench-overlay">
      <div class="workbench-panel">
        <!-- 标题栏 -->
        <div class="workbench-titlebar">
          <div class="workbench-title-group">
            <a-button type="text" size="small" class="lucide-icon-btn" title="返回" @click="close">
              <ArrowLeft :size="16" />
            </a-button>
            <span class="workbench-title">{{ dataset?.name }}</span>
            <span class="card-tag tag-gold">v{{ version }}</span>
            <span class="card-tag" :class="isDraft ? 'tag-blue' : 'tag-green'">
              {{ isDraft ? '编辑中' : '已完成（锁定）' }}
            </span>
            <span v-if="reviewRequired && isDraft" class="card-tag tag-blue">需要审核</span>
            <span v-if="parentDatasetId" class="card-tag">继承自 {{ parentDatasetId }}</span>
          </div>
          <a-button type="text" size="small" class="lucide-icon-btn" title="关闭" @click="close">
            <X :size="16" />
          </a-button>
        </div>

        <!-- 工具栏 -->
        <div class="workbench-toolbar">
          <div class="toolbar-left">
            <template v-if="isDraft">
              <a-button type="primary" size="small" @click="openForm(null)">
                <template #icon><Plus :size="14" /></template>
                新增题目
              </a-button>
              <a-button size="small" @click="importVisible = true">
                <template #icon><FileInput :size="14" /></template>
                批量导入
              </a-button>
              <a-button size="small" :loading="checkingDuplicates" @click="runDuplicateCheck">
                <template #icon><Copy :size="14" /></template>
                查重
              </a-button>
              <a-button size="small" :loading="checkingGold" @click="runGoldCheck">
                <template #icon><ShieldAlert :size="14" /></template>
                失效检测
              </a-button>
            </template>
            <a-button v-else size="small" @click="deriveNewVersion">
              <template #icon><GitBranch :size="14" /></template>
              派生新版本
            </a-button>
            <a-button size="small" @click="loadStats">
              <template #icon><BarChart3 :size="14" /></template>
              统计
            </a-button>
          </div>
          <div class="toolbar-right">
            <a-select
              v-model:value="statusFilter"
              size="small"
              allow-clear
              placeholder="状态"
              style="width: 110px"
              :options="statusOptions"
              @change="reloadItems"
            />
            <a-input-search
              v-model:value="keyword"
              size="small"
              placeholder="搜索问题/答案/编号"
              style="width: 220px"
              allow-clear
              @search="reloadItems"
            />
            <span class="status-counts">
              已审 <b class="ok">{{ statusCounts.approved || 0 }}</b> · 草稿
              <b>{{ statusCounts.draft || 0 }}</b> · 打回
              <b class="bad">{{ statusCounts.rejected || 0 }}</b>
            </span>
            <a-button
              v-if="isDraft"
              type="primary"
              size="small"
              :loading="finalizing"
              @click="runFinalize"
            >
              <template #icon><Lock :size="14" /></template>
              完成基准
            </a-button>
          </div>
        </div>

        <!-- 题目表 -->
        <div class="workbench-table">
          <a-table
            :data-source="items"
            :columns="columns"
            :pagination="pagination"
            :loading="itemsLoading"
            :scroll="{ x: 960, y: 'calc(100dvh - 232px)' }"
            size="small"
            row-key="item_id"
          >
            <template #bodyCell="{ column, record, index }">
              <template v-if="column.key === 'index'">
                <span class="row-num">{{ (page - 1) * pageSize + index + 1 }}</span>
              </template>
              <template v-else-if="column.key === 'status'">
                <a-tag :color="STATUS_COLORS[record.status]" :bordered="false">
                  {{ STATUS_LABELS[record.status] || record.status }}
                </a-tag>
              </template>
              <template v-else-if="column.key === 'query'">
                <div class="row-query" :title="record.query">{{ record.query }}</div>
                <div class="row-sub">
                  <span class="row-id">{{ record.external_id || record.item_id }}</span>
                  <span v-if="typeLabel(record)" class="row-type">{{ typeLabel(record) }}</span>
                  <span v-if="difficultyLabel(record)" class="row-type">{{
                    difficultyLabel(record)
                  }}</span>
                  <a-tooltip
                    v-if="rejectReasonOf(record)"
                    :title="`打回原因：${rejectReasonOf(record)}`"
                  >
                    <span class="row-reject">已打回</span>
                  </a-tooltip>
                </div>
              </template>
              <template v-else-if="column.key === 'gold_answer'">
                <div class="row-answer" :title="record.gold_answer">
                  {{ record.gold_answer || '—' }}
                </div>
              </template>
              <template v-else-if="column.key === 'tags'">
                <a-tag
                  v-for="tag in (record.item_metadata?.tags || []).slice(0, 3)"
                  :key="tag"
                  class="row-tag"
                >
                  {{ tag }}
                </a-tag>
                <span v-if="(record.item_metadata?.tags || []).length > 3" class="row-more">
                  +{{ record.item_metadata.tags.length - 3 }}
                </span>
              </template>
              <template v-else-if="column.key === 'gold_chunks'">
                <span v-if="record.gold_chunk_ids?.length"
                  >{{ record.gold_chunk_ids.length }} 块</span
                >
                <span v-else class="row-dim">—</span>
              </template>
              <template v-else-if="column.key === 'actions'">
                <div class="row-actions" @click.stop>
                  <template v-if="isDraft">
                    <a-button type="link" size="small" @click="openForm(record)">编辑</a-button>
                    <a-button
                      v-if="record.status !== 'approved'"
                      type="link"
                      size="small"
                      @click="reviewOne(record, 'approve')"
                    >
                      通过
                    </a-button>
                    <a-button
                      v-if="record.status !== 'rejected'"
                      type="link"
                      size="small"
                      danger
                      @click="reviewOne(record, 'reject')"
                    >
                      打回
                    </a-button>
                    <a-popconfirm
                      title="删除这道题？行序留空位不复用。"
                      @confirm="deleteItem(record)"
                    >
                      <a-button type="link" size="small" danger>删除</a-button>
                    </a-popconfirm>
                  </template>
                  <a-button v-else type="link" size="small" @click="openForm(record)"
                    >查看</a-button
                  >
                </div>
              </template>
            </template>
          </a-table>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- 题目表单 -->
  <BenchmarkItemForm
    v-model:visible="formVisible"
    :kb-id="kbId"
    :item="formItem"
    @saved="onItemSaved"
  />

  <!-- 批量导入 -->
  <a-modal
    v-model:open="importVisible"
    title="批量导入 JSONL（追加为草稿）"
    width="680px"
    :confirm-loading="importing"
    ok-text="导入"
    cancel-text="关闭"
    @ok="runImport"
  >
    <p class="modal-hint">
      每行一个 JSON 对象，必填 <code>query</code>，可选 <code>gold_answer</code> /
      <code>gold_chunk_ids</code> / <code>id</code> / <code>answer_type</code> /
      <code>tags</code> 等。合法行入库，非法行逐行报错，互不影响。
    </p>
    <a-textarea
      v-model:value="importContent"
      :rows="10"
      placeholder='{"query":"…","gold_answer":"…"}'
    />
    <div v-if="importResult" class="import-result">
      <a-alert
        :type="importResult.rejected ? 'warning' : 'success'"
        :message="`新增 ${importResult.added} 条，拒绝 ${importResult.rejected} 条`"
        show-icon
      />
      <div v-if="importResult.errors?.length" class="import-errors">
        <div v-for="error in importResult.errors" :key="error.line" class="import-error">
          第 {{ error.line }} 行：{{ error.message }}
        </div>
      </div>
    </div>
  </a-modal>

  <!-- 完成基准报告 -->
  <a-modal
    v-model:open="finalizeVisible"
    :title="finalizePassed ? '基准已完成并锁定' : '完成校验未通过'"
    width="720px"
    :footer="null"
  >
    <a-alert
      v-if="finalizePassed"
      type="success"
      show-icon
      message="基准已锁定为「已完成」，现在可以在 RAG 评估中发起评估。锁定后修改需派生新版本。"
      style="margin-bottom: 12px"
    />
    <a-alert
      v-if="!finalizePassed && finalizeReport?.errors?.length"
      type="error"
      show-icon
      :message="`${finalizeReport.errors.length} 项阻断问题`"
      style="margin-bottom: 12px"
    />
    <div class="report-section">
      <div v-if="finalizeReport?.errors?.length" class="report-list errors">
        <div v-for="(error, index) in finalizeReport.errors" :key="index" class="report-item">
          <a-tag color="error">{{ error.code }}</a-tag>
          <span>{{ error.message }}</span>
          <a-button
            v-if="error.item_ids?.length"
            type="link"
            size="small"
            @click="jumpToItem(error.item_ids[0])"
          >
            去修复
          </a-button>
        </div>
      </div>
      <div v-if="finalizeReport?.warnings?.length" class="report-list">
        <div v-for="(warning, index) in finalizeReport.warnings" :key="index" class="report-item">
          <a-tag color="warning">{{ warning.code }}</a-tag>
          <span>{{ warning.message }}</span>
        </div>
      </div>
      <div
        v-if="!finalizeReport?.errors?.length && !finalizeReport?.warnings?.length"
        class="report-empty"
      >
        无警告，全部通过。
      </div>
    </div>
  </a-modal>

  <!-- 查重结果 -->
  <a-modal v-model:open="duplicatesVisible" title="语义查重结果" width="720px" :footer="null">
    <div v-if="duplicatePairs.length" class="report-list">
      <div v-for="(pair, index) in duplicatePairs" :key="index" class="dup-pair">
        <div class="dup-meta">
          <a-tag :color="pair.exact ? 'error' : 'warning'">{{
            pair.exact ? '完全重复' : `相似 ${pair.similarity}`
          }}</a-tag>
        </div>
        <div class="dup-line">
          A（{{ pair.a.external_id || `#${pair.a.item_index + 1}` }}）：{{ pair.a.query }}
        </div>
        <div class="dup-line">
          B（{{ pair.b.external_id || `#${pair.b.item_index + 1}` }}）：{{ pair.b.query }}
        </div>
      </div>
    </div>
    <div v-else class="report-empty">没有发现近重复题目。</div>
  </a-modal>

  <!-- 失效参考块 -->
  <a-modal v-model:open="goldCheckVisible" title="失效参考块检测" width="720px" :footer="null">
    <div v-if="goldCheckResult?.stale_items?.length" class="report-list">
      <div v-for="stale in goldCheckResult.stale_items" :key="stale.item_id" class="report-item">
        <a-tag color="error">{{ stale.missing_chunk_ids.length }} 块失效</a-tag>
        <span class="stale-query"
          >{{ stale.external_id || `#${stale.item_index + 1}` }}：{{ stale.query }}</span
        >
        <div class="stale-ids">{{ stale.missing_chunk_ids.join('、') }}</div>
      </div>
      <p class="modal-hint">
        失效通常因为文档被删除或重新解析（块 ID 会变化）。请编辑题目重新选块。
      </p>
    </div>
    <div v-else class="report-empty">全部参考块有效。</div>
  </a-modal>

  <!-- 统计 -->
  <a-modal v-model:open="statsVisible" title="基准统计" width="640px" :footer="null">
    <div v-if="stats" class="stats-grid">
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-value">{{ stats.total }}</div>
          <div class="stat-label">题目总数</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ percent(stats.gold_answer_coverage) }}</div>
          <div class="stat-label">参考答案覆盖</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ percent(stats.gold_chunk_coverage) }}</div>
          <div class="stat-label">参考块覆盖</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ stats.unanswerable_count }}</div>
          <div class="stat-label">不可回答题</div>
        </div>
      </div>
      <div class="stats-section">
        <div class="stats-title">状态</div>
        <div class="stats-tags">
          <a-tag
            v-for="(count, status) in stats.by_status"
            :key="status"
            :color="STATUS_COLORS[status]"
          >
            {{ STATUS_LABELS[status] || status }}：{{ count }}
          </a-tag>
        </div>
      </div>
      <div class="stats-section">
        <div class="stats-title">标签分布</div>
        <div class="stats-tags">
          <a-tag v-for="(count, tag) in stats.by_tag" :key="tag">{{ tag }}：{{ count }}</a-tag>
          <span v-if="!Object.keys(stats.by_tag || {}).length" class="report-empty">无标签</span>
        </div>
      </div>
      <div class="stats-section">
        <div class="stats-title">答案类型</div>
        <div class="stats-tags">
          <a-tag v-for="(count, type) in stats.by_answer_type" :key="type"
            >{{ type }}：{{ count }}</a-tag
          >
        </div>
      </div>
    </div>
  </a-modal>
</template>

<script setup>
import { ref, computed, h, watch } from 'vue'
import { message, Modal, Input } from 'ant-design-vue'
import {
  ArrowLeft,
  BarChart3,
  Copy,
  FileInput,
  GitBranch,
  Lock,
  Plus,
  ShieldAlert,
  X
} from '@lucide/vue'
import { evaluationApi } from '@/apis/knowledge_api'
import BenchmarkItemForm from './modals/BenchmarkItemForm.vue'

const STATUS_LABELS = { draft: '草稿', approved: '已审', rejected: '打回' }
const STATUS_COLORS = { draft: 'default', approved: 'success', rejected: 'error' }
const TYPE_LABELS = {
  fact: '事实',
  numeric: '数值',
  list: '列表',
  boolean: '是否',
  procedure: '流程',
  unanswerable: '不可回答'
}
const DIFFICULTY_LABELS = { easy: '易', medium: '中', hard: '难' }

const props = defineProps({
  visible: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  dataset: { type: Object, default: null }
})

const emit = defineEmits(['update:visible', 'changed'])

const items = ref([])
const itemsLoading = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statusFilter = ref(null)
const keyword = ref('')
const statusCounts = ref({})

const formVisible = ref(false)
const formItem = ref(null)

const importVisible = ref(false)
const importing = ref(false)
const importContent = ref('')
const importResult = ref(null)

const finalizing = ref(false)
const finalizeVisible = ref(false)
const finalizePassed = ref(false)
const finalizeReport = ref(null)

const checkingDuplicates = ref(false)
const duplicatesVisible = ref(false)
const duplicatePairs = ref([])

const checkingGold = ref(false)
const goldCheckVisible = ref(false)
const goldCheckResult = ref(null)

const statsVisible = ref(false)
const stats = ref(null)

const isDraft = computed(() => props.dataset?.status === 'draft')
const reviewRequired = computed(() => !!props.dataset?.build_metadata?.review_required)
const version = computed(() => props.dataset?.build_metadata?.version || 1)
const parentDatasetId = computed(() => props.dataset?.build_metadata?.parent_dataset_id)

const statusOptions = [
  { value: 'draft', label: '草稿' },
  { value: 'approved', label: '已审' },
  { value: 'rejected', label: '打回' }
]

const columns = computed(() => {
  const base = [
    { title: '#', key: 'index', width: 52, align: 'center' },
    { title: '状态', key: 'status', width: 72, align: 'center' },
    { title: '问题', key: 'query', width: 320 },
    { title: '参考答案', key: 'gold_answer', width: 260 },
    { title: '标签', key: 'tags', width: 140 },
    { title: '参考块', key: 'gold_chunks', width: 76, align: 'center' }
  ]
  base.push({ title: '操作', key: 'actions', width: isDraft.value ? 210 : 76, fixed: 'right' })
  return base
})

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showTotal: (count) => `共 ${count} 条`,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  size: 'small',
  onChange: (next) => {
    page.value = next
    loadItems()
  },
  onShowSizeChange: (_, size) => {
    pageSize.value = size
    page.value = 1
    loadItems()
  }
}))

const typeLabel = (record) => TYPE_LABELS[record.item_metadata?.answer_type]
const difficultyLabel = (record) => DIFFICULTY_LABELS[record.item_metadata?.difficulty]
const rejectReasonOf = (record) =>
  record.status === 'rejected'
    ? record.item_metadata?.reject_reason || record.item_metadata?.review_history?.at(-1)?.reason
    : null

const loadItems = async () => {
  if (!props.dataset?.dataset_id) return
  itemsLoading.value = true
  try {
    const params = new URLSearchParams({ page: page.value, page_size: pageSize.value })
    if (statusFilter.value) params.append('status', statusFilter.value)
    if (keyword.value?.trim()) params.append('keyword', keyword.value.trim())
    const response = await evaluationApi.getDataset(
      props.kbId,
      props.dataset.dataset_id,
      page.value,
      pageSize.value,
      statusFilter.value,
      keyword.value?.trim()
    )
    if (response.message === 'success') {
      items.value = response.data.items || []
      total.value = response.data.pagination?.total_items || 0
    }
  } catch {
    message.error('题目列表加载失败')
  } finally {
    itemsLoading.value = false
  }
}

const loadStatusCounts = async () => {
  try {
    const response = await evaluationApi.getDatasetStats(props.dataset.dataset_id)
    if (response.message === 'success') {
      statusCounts.value = response.data.by_status || {}
    }
  } catch {
    statusCounts.value = {}
  }
}

const reloadItems = () => {
  page.value = 1
  loadItems()
  loadStatusCounts()
}

const openForm = (record) => {
  formItem.value = record
    ? { ...record, datasetId: props.dataset.dataset_id }
    : { datasetId: props.dataset.dataset_id }
  formVisible.value = true
}

const onItemSaved = () => {
  loadItems()
  loadStatusCounts()
  emit('changed')
}

const deleteItem = async (record) => {
  try {
    const response = await evaluationApi.deleteDatasetItem(props.dataset.dataset_id, record.item_id)
    if (response.message === 'success') {
      message.success('已删除')
      loadItems()
      loadStatusCounts()
      emit('changed')
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || '删除失败')
  }
}

const reviewOne = (record, action) => {
  if (action === 'approve') {
    doReview([record.item_id], action, '')
    return
  }
  let reason = ''
  Modal.confirm({
    title: `打回「${record.external_id || record.query.slice(0, 24)}」`,
    content: () =>
      h('div', null, [
        h(
          'p',
          { style: 'margin:0 0 8px;font-size:13px;color:var(--gray-600)' },
          '请填写打回原因（必填，作者在题目编辑页可见）：'
        ),
        h(Input, {
          value: reason,
          'onUpdate:value': (value) => {
            reason = value
          },
          placeholder: '如：答案与文档不符，请核对第 5 页'
        })
      ]),
    okText: '打回',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      if (!reason.trim()) {
        message.warning('打回必须填写原因')
        return Promise.reject()
      }
      await doReview([record.item_id], action, reason.trim())
    }
  })
}

const doReview = async (itemIds, action, reason) => {
  try {
    const response = await evaluationApi.reviewDatasetItems(props.dataset.dataset_id, {
      item_ids: itemIds,
      action,
      reason
    })
    if (response.message === 'success') {
      message.success(
        action === 'approve' ? '已通过' : action === 'reject' ? '已打回' : '已重置为草稿'
      )
      loadItems()
      loadStatusCounts()
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || '审核操作失败')
  }
}

const runImport = async () => {
  if (!importContent.value.trim()) {
    message.warning('请输入 JSONL 内容')
    return
  }
  importing.value = true
  try {
    const response = await evaluationApi.batchImportItems(
      props.dataset.dataset_id,
      importContent.value
    )
    if (response.message === 'success') {
      importResult.value = response.data
      loadItems()
      loadStatusCounts()
      emit('changed')
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || '导入失败')
  } finally {
    importing.value = false
  }
}

const runFinalize = async () => {
  finalizing.value = true
  try {
    const response = await evaluationApi.finalizeDataset(props.dataset.dataset_id)
    if (response.message === 'success') {
      finalizePassed.value = true
      finalizeReport.value = response.data.report
      finalizeVisible.value = true
      emit('changed')
      close()
    }
  } catch (error) {
    const detail = error?.response?.data?.detail
    if (detail?.report) {
      finalizePassed.value = false
      finalizeReport.value = detail.report
      finalizeVisible.value = true
    } else {
      message.error(typeof detail === 'string' ? detail : '完成基准失败')
    }
  } finally {
    finalizing.value = false
  }
}

const jumpToItem = (itemId) => {
  finalizeVisible.value = false
  statusFilter.value = null
  keyword.value = ''
  page.value = 1
  loadItems().then(() => {
    const record = items.value.find((item) => item.item_id === itemId)
    if (record) openForm(record)
  })
}

const runDuplicateCheck = async () => {
  checkingDuplicates.value = true
  try {
    const response = await evaluationApi.checkDuplicates(props.dataset.dataset_id, {
      threshold: 0.92
    })
    if (response.message === 'success') {
      duplicatePairs.value = response.data.pairs || []
      duplicatesVisible.value = true
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || '查重失败')
  } finally {
    checkingDuplicates.value = false
  }
}

const runGoldCheck = async () => {
  checkingGold.value = true
  try {
    const response = await evaluationApi.checkGoldChunks(props.dataset.dataset_id)
    if (response.message === 'success') {
      goldCheckResult.value = response.data
      goldCheckVisible.value = true
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || '失效检测失败')
  } finally {
    checkingGold.value = false
  }
}

const loadStats = async () => {
  try {
    const response = await evaluationApi.getDatasetStats(props.dataset.dataset_id)
    if (response.message === 'success') {
      stats.value = response.data
      statsVisible.value = true
    }
  } catch {
    message.error('统计加载失败')
  }
}

const deriveNewVersion = () => {
  Modal.confirm({
    title: '派生新版本',
    content: '将复制全部题目为新的「编辑中」基准，当前版本保持锁定不可变。',
    okText: '派生',
    cancelText: '取消',
    onOk: async () => {
      try {
        const response = await evaluationApi.createDatasetVersion(props.dataset.dataset_id, {})
        if (response.message === 'success') {
          message.success(`已派生 v${response.data.build_metadata.version}，正在打开新版本`)
          emit('changed', response.data)
        }
      } catch (error) {
        message.error(error?.response?.data?.detail || '派生失败')
      }
    }
  })
}

const percent = (value) => `${Math.round((value || 0) * 100)}%`

const close = () => {
  emit('update:visible', false)
}

watch(
  () => props.visible,
  (open) => {
    if (open) {
      statusFilter.value = null
      keyword.value = ''
      importResult.value = null
      importContent.value = ''
      page.value = 1
      loadItems()
      loadStatusCounts()
    }
  }
)
</script>

<style lang="less" scoped>
:global(.workbench-overlay) {
  position: fixed;
  inset: 0;
  z-index: 1000;
  width: 100vw;
  height: 100dvh;
  padding: 12px;
  box-sizing: border-box;
  background: var(--dark-25);
  overflow: hidden;
}

:global(.workbench-panel) {
  width: 100%;
  height: calc(100dvh - 24px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 12px;
  background: var(--color-bg-container);
  box-shadow: var(--shadow-4);
}

.workbench-titlebar {
  height: 44px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 12px 0 8px;
  border-bottom: 1px solid var(--gray-150);
}

.workbench-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;

  .workbench-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--gray-1000);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.workbench-toolbar {
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 16px;

  .toolbar-left,
  .toolbar-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .status-counts {
    font-size: 12px;
    color: var(--gray-500);

    b {
      font-weight: 600;

      &.ok {
        color: var(--color-success-700, #389e0d);
      }

      &.bad {
        color: var(--color-error-700, #cf1322);
      }
    }
  }
}

.workbench-table {
  flex: 1;
  min-height: 0;
  padding: 0 16px 12px;

  .row-num {
    font-weight: 600;
    color: var(--gray-500);
  }

  .row-query {
    font-size: 13px;
    color: var(--gray-800);
    line-height: 1.5;
    word-break: break-all;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .row-sub {
    display: flex;
    gap: 8px;
    margin-top: 2px;
    font-size: 11px;
    color: var(--gray-400);

    .row-reject {
      color: var(--color-error-700, #cf1322);
    }
  }

  .row-answer {
    font-size: 12px;
    color: var(--gray-600);
    line-height: 1.5;
    word-break: break-all;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .row-dim {
    color: var(--gray-300);
  }

  .row-tag {
    font-size: 11px;
  }

  .row-actions {
    display: flex;
    gap: 0;
  }
}

.modal-hint {
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 10px;

  code {
    background: var(--gray-100);
    padding: 1px 4px;
    border-radius: 3px;
  }
}

.import-result {
  margin-top: 12px;

  .import-errors {
    max-height: 180px;
    overflow-y: auto;
    margin-top: 8px;
    font-size: 12px;

    .import-error {
      color: var(--color-error-700, #cf1322);
      padding: 2px 0;
    }
  }
}

.report-list {
  max-height: 380px;
  overflow-y: auto;

  .report-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 0;
    font-size: 13px;
    color: var(--gray-700);
    border-bottom: 1px dashed var(--gray-100);
    flex-wrap: wrap;

    .stale-query {
      flex: 1;
      min-width: 0;
    }

    .stale-ids {
      width: 100%;
      font-size: 11px;
      color: var(--gray-400);
      font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
      word-break: break-all;
    }
  }
}

.report-empty {
  text-align: center;
  color: var(--gray-400);
  padding: 24px 0;
  font-size: 13px;
}

.dup-pair {
  padding: 8px 0;
  border-bottom: 1px dashed var(--gray-100);

  .dup-line {
    font-size: 13px;
    color: var(--gray-700);
    padding: 2px 0;
    word-break: break-all;
  }
}

.stats-grid {
  .stats-row {
    display: flex;
    gap: 10px;
    margin-bottom: 16px;

    .stat-card {
      flex: 1;
      text-align: center;
      padding: 12px 0;
      border: 1px solid var(--gray-150);
      border-radius: 8px;

      .stat-value {
        font-size: 22px;
        font-weight: 600;
        color: var(--gray-1000);
      }

      .stat-label {
        font-size: 12px;
        color: var(--gray-500);
        margin-top: 2px;
      }
    }
  }

  .stats-section {
    margin-bottom: 12px;

    .stats-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-800);
      margin-bottom: 6px;
    }

    .stats-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
  }
}
</style>
