<template>
  <a-modal
    :open="open"
    title="托管图谱导入"
    width="920px"
    :footer="null"
    @cancel="close"
  >
    <a-alert
      type="info"
      show-icon
      class="import-notice"
      message="PostgreSQL 是规范数据源，Neo4j 与 Milvus 是可重建投影"
      description="支持节点 CSV、关系 CSV 和可选 Cypher 说明文件。Cypher 只保存、分析与审计，永不执行。"
    />

    <a-tabs v-model:active-key="activeTab">
      <a-tab-pane key="new" tab="新建导入">
        <a-form layout="vertical">
          <a-form-item label="批次名称">
            <a-input v-model:value="batchName" :maxlength="255" placeholder="例如：水稻胚乳图谱 v3" />
          </a-form-item>
          <div class="file-grid">
            <FilePicker
              title="节点 CSV"
              hint="必需 · UTF-8 · 最大 100 MB"
              accept=".csv,text/csv"
              :file="nodesFile"
              @change="nodesFile = $event"
            />
            <FilePicker
              title="关系 CSV"
              hint="必需 · UTF-8 · 最大 100 MB"
              accept=".csv,text/csv"
              :file="relationshipsFile"
              @change="relationshipsFile = $event"
            />
            <FilePicker
              title="Cypher 说明"
              hint="可选 · 仅审计，永不执行"
              accept=".cypher,text/plain"
              :file="cypherFile"
              @change="cypherFile = $event"
            />
          </div>
          <a-button
            type="primary"
            class="primary-action"
            :loading="uploading"
            :disabled="!nodesFile || !relationshipsFile"
            @click="uploadAndValidate"
          >
            <Upload :size="16" />
            上传并预检
          </a-button>
        </a-form>

        <section v-if="currentImport" class="report-card">
          <div class="report-header">
            <div>
              <div class="report-title">{{ currentImport.name }}</div>
              <div class="report-id">{{ currentImport.import_id }}</div>
            </div>
            <a-tag :color="statusMeta.color">{{ statusMeta.label }}</a-tag>
          </div>

          <a-progress
            v-if="isRunning"
            :percent="taskProgress"
            status="active"
            :show-info="true"
          />
          <a-alert
            v-if="currentImport.error_message"
            type="error"
            show-icon
            class="report-alert"
            :message="currentImport.error_message"
          />
          <a-alert
            v-if="currentImport.status === 'SUCCEEDED' && currentImport.result?.reconciliation?.matched"
            type="success"
            show-icon
            class="report-alert"
            message="规范库与双投影已完成精确 ID 对账"
            :description="reconciliationDescription"
          />

          <div v-if="report?.counts" class="count-grid">
            <div v-for="item in countItems" :key="item.label" class="count-item">
              <span class="count-value">{{ item.value }}</span>
              <span class="count-label">{{ item.label }}</span>
            </div>
          </div>

          <a-alert
            v-if="report?.cypher?.provided"
            type="warning"
            show-icon
            class="report-alert"
            message="Cypher 已进入审计区，执行权限为关闭"
            :description="`检测到 ${report.cypher.statement_count} 个语句片段；写操作关键词：${(report.cypher.write_keywords || []).join('、') || '无'}`"
          />

          <div v-if="report?.blockers?.length" class="issue-list">
            <div class="issue-title error">阻塞错误（{{ report.blockers.length }}）</div>
            <div v-for="item in report.blockers.slice(0, 8)" :key="issueKey(item)" class="issue-row">
              <span>{{ item.code }}</span>
              <span>{{ item.message }}</span>
              <span v-if="item.row_number">第 {{ item.row_number }} 行</span>
            </div>
          </div>

          <div v-if="generalWarnings.length" class="issue-list">
            <div class="issue-title">预检警告（{{ report.warnings.length }}）</div>
            <div
              v-for="item in generalWarnings.slice(0, 6)"
              :key="issueKey(item)"
              class="issue-row"
            >
              <span>{{ item.code }}</span>
              <span>{{ item.message }}</span>
              <span v-if="item.row_number">第 {{ item.row_number }} 行</span>
            </div>
            <div v-if="generalWarnings.length > 6" class="more-hint">
              另有 {{ generalWarnings.length - 6 }} 条警告已完整保存在导入记录中
            </div>
          </div>

          <details v-if="caseReviews.length" class="review-disclosure">
            <summary>大小写待核验（{{ caseReviews.length }}）· 非阻塞</summary>
            <p class="conflict-help">
              系统已按规范名称自动合并并保留全部别名。可选择展示名，也可前往 RAP-DB / Rice Genome Annotation Project 核验；不选择不会阻止导入。
            </p>
            <div v-for="review in caseReviews" :key="review.review_id" class="case-review-row">
              <div>
                <strong>{{ review.external_ids.join('、') }}</strong>
                <span class="case-aliases">{{ review.aliases.join(' / ') }}</span>
              </div>
              <a-radio-group v-model:value="caseReviewSelections[review.review_id]" size="small">
                <a-radio-button
                  v-for="option in review.options"
                  :key="option.row_number"
                  :value="option.row_number"
                  :disabled="!reviewEditable"
                >
                  {{ option.name }}
                </a-radio-button>
              </a-radio-group>
              <div class="registry-links">
                <a href="https://rapdb.dna.naro.go.jp/" target="_blank" rel="noopener noreferrer">RAP-DB</a>
                <a href="https://rice.uga.edu/" target="_blank" rel="noopener noreferrer">RGAP</a>
              </div>
            </div>
          </details>

          <div v-if="report?.conflicts?.length" class="conflict-section">
            <div class="issue-title error">
              需要人工选择（{{ report.conflicts.length }}）
            </div>
            <p class="conflict-help">
              系统不会猜测大小写名称或把基因与突变体自动合并。语义类型冲突会保留为不同实体；这里选择关系端点默认指向的记录。
            </p>
            <div
              v-for="conflict in report.conflicts"
              :key="conflict.conflict_id"
              class="conflict-card"
            >
              <div class="conflict-heading">
                <span>{{ conflict.code }}</span>
                <span>{{ conflict.external_ids.join('、') }}</span>
              </div>
              <a-alert
                v-if="conflict.resolvable === false"
                type="error"
                show-icon
                message="该错误不能通过选择记录绕过，请修正 CSV 后重新上传"
              />
              <a-radio-group v-else v-model:value="resolutionSelections[conflict.conflict_id]">
                <a-radio
                  v-for="option in conflict.options"
                  :key="option.row_number"
                  :value="option.row_number"
                >
                  第 {{ option.row_number }} 行 · {{ option.name }} · {{ option.node_type }}
                  <span v-if="option.rap_id || option.msu_id" class="registry-id">
                    {{ option.rap_id || option.msu_id }}
                  </span>
                </a-radio>
              </a-radio-group>
            </div>
          </div>

          <div v-if="report?.semantic_splits?.length" class="semantic-section">
            <div class="semantic-title-row">
              <div>
                <div class="issue-title safe">安全拆分方案（{{ report.semantic_splits.length }}）</div>
                <p class="conflict-help">默认保留 Gene 与 AlleleMutant 两个实体、建立 ALLELE_OF，并按证据语义逐行路由。以下方案可直接导入，也可逐关系覆盖。</p>
              </div>
              <a-tag color="green">科研语义安全</a-tag>
            </div>
            <details
              v-for="split in report.semantic_splits"
              :key="split.split_id"
              class="split-card"
            >
              <summary>
                {{ split.preview.gene.name }} / {{ split.preview.allele.name }}
                <span>{{ split.relation_routes.length }} 条关系 · 自动增加 ALLELE_OF</span>
              </summary>
              <div class="entity-preview-grid">
                <label class="entity-preview gene-preview">
                  <span>Gene</span>
                  <a-input v-model:value="semanticResolutions[split.split_id].gene_name" size="small" :disabled="!reviewEditable" />
                  <code>{{ split.preview.gene.canonical_identity }}</code>
                </label>
                <div class="allele-arrow">ALLELE_OF →</div>
                <label class="entity-preview allele-preview">
                  <span>AlleleMutant</span>
                  <a-input v-model:value="semanticResolutions[split.split_id].allele_name" size="small" :disabled="!reviewEditable" />
                  <code>{{ split.preview.allele.canonical_identity }}</code>
                </label>
              </div>
              <div class="route-table">
                <div v-for="route in split.relation_routes" :key="route.row_number" class="route-row">
                  <div>
                    <strong>第 {{ route.row_number }} 行 · {{ route.relation_type }}</strong>
                    <span>{{ route.start_id }} → {{ route.end_id }}</span>
                    <small>{{ route.reason }}</small>
                  </div>
                  <label v-for="(_, endpoint) in route.endpoints" :key="endpoint">
                    {{ endpoint === 'start' ? '起点' : '终点' }}
                    <a-select
                      v-model:value="semanticResolutions[split.split_id].relation_routes[String(route.row_number)][endpoint]"
                      size="small"
                      style="width: 130px"
                      :disabled="!reviewEditable"
                    >
                      <a-select-option value="gene">Gene</a-select-option>
                      <a-select-option value="allele">AlleleMutant</a-select-option>
                    </a-select>
                  </label>
                </div>
              </div>
            </details>
          </div>

          <details v-if="report?.info?.length" class="review-disclosure info-disclosure">
            <summary>审计信息（{{ report.info.length }}）</summary>
            <div v-for="item in report.info" :key="issueKey(item)" class="issue-row">
              <span>{{ item.code }}</span>
              <span>{{ item.message }}</span>
              <span v-if="item.count">{{ item.count }} 条</span>
            </div>
          </details>

          <div class="report-actions">
            <a-button
              v-if="hasReviewablePlan"
              :loading="validating"
              :disabled="!allBlockingConflictsResolved"
              @click="revalidate"
            >
              <ShieldCheck :size="16" />
              应用审阅方案并重新预检
            </a-button>
            <a-button
              v-if="canExecute"
              type="primary"
              :loading="executing"
              @click="executeImport"
            >
              <Play :size="16" />
              开始后台导入
            </a-button>
            <a-button @click="refreshCurrent" :loading="refreshing">
              <RefreshCw :size="16" />
              刷新状态
            </a-button>
          </div>
        </section>
      </a-tab-pane>

      <a-tab-pane key="history" tab="导入历史">
        <div class="history-toolbar">
          <span>原始文件与 SHA256 永久保留，便于审计和重复验证。</span>
          <a-button size="small" :loading="historyLoading" @click="loadHistory">
            <RefreshCw :size="14" />
            刷新
          </a-button>
        </div>
        <a-table
          size="small"
          row-key="import_id"
          :columns="historyColumns"
          :data-source="history"
          :pagination="{ pageSize: 8 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'status'">
              <a-tag :color="getStatusMeta(record.status).color">
                {{ getStatusMeta(record.status).label }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'counts'">
              {{ record.result?.entity_count ?? '-' }} / {{ record.result?.triple_count ?? '-' }}
            </template>
            <template v-else-if="column.key === 'created_at'">
              {{ formatDate(record.created_at) }}
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space>
                <a-button type="link" size="small" @click="viewImport(record)">详情</a-button>
                <a-button
                  v-if="record.status === 'SUCCEEDED'"
                  type="link"
                  size="small"
                  danger
                  @click="confirmRollback(record)"
                >
                  回滚
                </a-button>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>
  </a-modal>
</template>

<script setup>
import { computed, defineComponent, h, onUnmounted, reactive, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { FileSpreadsheet, Play, RefreshCw, ShieldCheck, Upload } from 'lucide-vue-next'
import { graphImportApi } from '@/apis/knowledge_api'
import { useTaskerStore } from '@/stores/tasker'

const props = defineProps({
  open: { type: Boolean, default: false },
  kbId: { type: String, required: true }
})
const emit = defineEmits(['update:open', 'imported'])
const taskerStore = useTaskerStore()

const activeTab = ref('new')
const batchName = ref('水稻胚乳托管图谱导入')
const nodesFile = ref(null)
const relationshipsFile = ref(null)
const cypherFile = ref(null)
const currentImport = ref(null)
const history = ref([])
const resolutionSelections = reactive({})
const caseReviewSelections = reactive({})
const semanticResolutions = reactive({})
const uploading = ref(false)
const validating = ref(false)
const executing = ref(false)
const refreshing = ref(false)
const historyLoading = ref(false)
const taskProgress = ref(0)
let pollTimer = null

const ACTIVE_STATUSES = new Set([
  'UPLOADED',
  'PARSING',
  'VALIDATING',
  'IMPORTING',
  'PROJECTING_NEO4J',
  'PROJECTING_MILVUS',
  'RECONCILING',
  'ROLLING_BACK'
])

const STATUS_META = {
  UPLOADED: ['已上传', 'blue'],
  PARSING: ['解析中', 'processing'],
  VALIDATING: ['预检中', 'processing'],
  AWAITING_CONFLICT_RESOLUTION: ['待解决冲突', 'orange'],
  READY: ['预检通过', 'green'],
  IMPORTING: ['写入规范库', 'processing'],
  PROJECTING_NEO4J: ['投影 Neo4j', 'processing'],
  PROJECTING_MILVUS: ['投影 Milvus', 'processing'],
  RECONCILING: ['三库对账', 'processing'],
  SUCCEEDED: ['已完成', 'green'],
  FAILED: ['失败', 'red'],
  CANCELLED: ['已取消', 'default'],
  ROLLING_BACK: ['回滚中', 'processing'],
  ROLLED_BACK: ['已回滚', 'default']
}

const historyColumns = [
  { title: '批次', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '状态', key: 'status', width: 110 },
  { title: '实体 / 关系', key: 'counts', width: 120 },
  { title: '创建时间', key: 'created_at', width: 170 },
  { title: '操作', key: 'actions', width: 120 }
]

const FilePicker = defineComponent({
  props: {
    title: String,
    hint: String,
    accept: String,
    file: Object
  },
  emits: ['change'],
  setup(componentProps, { emit: emitFile }) {
    const choose = (event) => emitFile('change', event.target.files?.[0] || null)
    return () =>
      h('label', { class: ['file-picker', { selected: componentProps.file }] }, [
        h(FileSpreadsheet, { size: 22 }),
        h('span', { class: 'file-title' }, componentProps.title),
        h('span', { class: 'file-name' }, componentProps.file?.name || '点击选择文件'),
        h('span', { class: 'file-hint' }, componentProps.hint),
        h('input', { type: 'file', accept: componentProps.accept, onChange: choose })
      ])
  }
})

const report = computed(() => currentImport.value?.validation_report || null)
const statusMeta = computed(() => getStatusMeta(currentImport.value?.status))
const isRunning = computed(() => ACTIVE_STATUSES.has(currentImport.value?.status))
const reviewEditable = computed(() => !['SUCCEEDED', 'ROLLED_BACK'].includes(currentImport.value?.status))
const canExecute = computed(
  () => report.value?.valid && ['READY', 'FAILED', 'CANCELLED'].includes(currentImport.value?.status)
)
const caseReviews = computed(() =>
  (report.value?.warnings || []).filter((item) => item.code === 'CASE_UNRESOLVED' && item.review_id)
)
const generalWarnings = computed(() =>
  (report.value?.warnings || []).filter((item) => item.code !== 'CASE_UNRESOLVED')
)
const allBlockingConflictsResolved = computed(() =>
  (report.value?.conflicts || []).every(
    (item) => item.resolvable !== false && resolutionSelections[item.conflict_id]
  )
)
const hasReviewablePlan = computed(
  () =>
    reviewEditable.value &&
    (Boolean(report.value?.conflicts?.length) ||
      Boolean(report.value?.semantic_splits?.length) ||
      Boolean(caseReviews.value.length))
)
const countItems = computed(() => {
  const counts = report.value?.counts || {}
  return [
    ['节点行', counts.node_rows],
    ['规范实体', counts.canonical_entities],
    ['关系行', counts.relationship_rows],
    ['规范三元组', counts.canonical_triples],
    ['证据断言', counts.evidence_assertions],
    ['阻塞冲突', counts.unresolved_conflicts]
  ].map(([label, value]) => ({ label, value: value ?? 0 }))
})
const reconciliationDescription = computed(() => {
  const reconciliation = currentImport.value?.result?.reconciliation
  if (!reconciliation?.matched) return ''
  return `PostgreSQL ${reconciliation.expected.entities} 个实体 / ${reconciliation.expected.triples} 个三元组；Neo4j ${reconciliation.neo4j.entities} / ${reconciliation.neo4j.triples}；Milvus ${reconciliation.milvus.entities} / ${reconciliation.milvus.triples}。缺失 ID 为 0。`
})

watch(
  () => props.open,
  (open) => {
    if (open) loadHistory()
    else stopPolling()
  }
)

watch(
  () => props.kbId,
  () => {
    currentImport.value = null
    history.value = []
    stopPolling()
  }
)

const uploadAndValidate = async () => {
  uploading.value = true
  try {
    const response = await graphImportApi.upload(props.kbId, {
      name: batchName.value,
      nodesFile: nodesFile.value,
      relationshipsFile: relationshipsFile.value,
      cypherFile: cypherFile.value
    })
    currentImport.value = response.data
    hydrateResolutions()
    message.success(response.deduplicated ? '检测到相同文件，已打开已有导入批次' : '上传完成，预检报告已生成')
    await loadHistory()
  } catch (error) {
    message.error(error.message || '上传与预检失败')
  } finally {
    uploading.value = false
  }
}

const revalidate = async () => {
  validating.value = true
  try {
    const appliedResolutions = buildResolutions()
    const validationReport = await graphImportApi.validate(
      props.kbId,
      currentImport.value.import_id,
      appliedResolutions
    )
    currentImport.value = {
      ...currentImport.value,
      status: validationReport.status,
      validation_report: validationReport,
      resolution_config: appliedResolutions,
      error_message: validationReport.blockers?.[0]?.message || null
    }
    hydrateResolutions()
    message.success(validationReport.valid ? '预检通过，可以开始导入' : '已更新预检报告')
    await loadHistory()
  } catch (error) {
    message.error(error.message || '重新预检失败')
  } finally {
    validating.value = false
  }
}

const executeImport = async () => {
  executing.value = true
  try {
    const response = await graphImportApi.execute(
      props.kbId,
      currentImport.value.import_id,
      buildResolutions()
    )
    taskerStore.registerQueuedTask({
      task_id: response.task_id,
      name: `托管图谱导入 (${currentImport.value.name})`,
      task_type: 'knowledge_graph_import',
      message: response.message,
      payload: { kb_id: props.kbId, import_id: currentImport.value.import_id }
    })
    taskProgress.value = 1
    message.success('导入已交给后台任务，切换页面不会中断')
    startPolling()
  } catch (error) {
    message.error(error.message || '提交导入任务失败')
  } finally {
    executing.value = false
  }
}

const refreshCurrent = async () => {
  if (!currentImport.value) return
  refreshing.value = true
  try {
    const latest = await graphImportApi.get(props.kbId, currentImport.value.import_id)
    const previousStatus = currentImport.value.status
    currentImport.value = latest
    taskProgress.value = estimateProgress(latest.status)
    if (latest.status === 'SUCCEEDED' && previousStatus !== 'SUCCEEDED') {
      message.success('导入完成，PostgreSQL、Neo4j 与 Milvus ID 对账一致')
      emit('imported')
      await loadHistory()
    }
    if (ACTIVE_STATUSES.has(latest.status)) startPolling()
  } catch (error) {
    message.error(error.message || '刷新导入状态失败')
  } finally {
    refreshing.value = false
  }
}

const loadHistory = async () => {
  if (!props.kbId) return
  historyLoading.value = true
  try {
    const response = await graphImportApi.list(props.kbId)
    history.value = response.items || []
  } catch (error) {
    message.error(error.message || '加载导入历史失败')
  } finally {
    historyLoading.value = false
  }
}

const viewImport = (record) => {
  currentImport.value = record
  hydrateResolutions()
  activeTab.value = 'new'
  if (ACTIVE_STATUSES.has(record.status)) startPolling()
}

const confirmRollback = (record) => {
  Modal.confirm({
    title: '确认回滚这个导入批次？',
    content: '只会删除失去全部来源的数据；其他导入或文档仍引用的数据会保留，并重新校准 Neo4j/Milvus 投影。',
    okText: '安全回滚',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        const response = await graphImportApi.rollback(props.kbId, record.import_id)
        taskerStore.registerQueuedTask({
          task_id: response.task_id,
          name: `回滚图谱导入 (${record.name})`,
          task_type: 'knowledge_graph_import_rollback',
          message: response.message,
          payload: { kb_id: props.kbId, import_id: record.import_id }
        })
        currentImport.value = { ...record, status: 'ROLLING_BACK' }
        activeTab.value = 'new'
        startPolling()
      } catch (error) {
        message.error(error.message || '提交回滚任务失败')
      }
    }
  })
}

const hydrateResolutions = () => {
  Object.keys(resolutionSelections).forEach((key) => delete resolutionSelections[key])
  Object.keys(caseReviewSelections).forEach((key) => delete caseReviewSelections[key])
  Object.keys(semanticResolutions).forEach((key) => delete semanticResolutions[key])
  const saved = currentImport.value?.resolution_config || {}
  for (const conflict of report.value?.conflicts || []) {
    const value = saved[conflict.conflict_id]
    if (value) resolutionSelections[conflict.conflict_id] = value.selected_row_number || value
  }
  for (const review of caseReviews.value) {
    const value = saved[review.review_id]
    if (value) caseReviewSelections[review.review_id] = value.selected_row_number || value
  }
  for (const split of report.value?.semantic_splits || []) {
    const value = saved[split.split_id] || {}
    const relationRoutes = {}
    for (const route of split.relation_routes || []) {
      const savedRoute = value.relation_routes?.[String(route.row_number)] || {}
      relationRoutes[String(route.row_number)] = Object.fromEntries(
        Object.entries(route.endpoints).map(([endpoint, role]) => [endpoint, savedRoute[endpoint] || role])
      )
    }
    semanticResolutions[split.split_id] = {
      action: 'split',
      gene_name: value.gene_name || split.preview.gene.name,
      allele_name: value.allele_name || split.preview.allele.name,
      relation_routes: relationRoutes
    }
  }
}

const buildResolutions = () => ({
  ...Object.fromEntries(
    Object.entries(resolutionSelections).map(([conflictId, selectedRowNumber]) => [
      conflictId,
      { selected_row_number: Number(selectedRowNumber) }
    ])
  ),
  ...Object.fromEntries(
    Object.entries(caseReviewSelections).map(([reviewId, selectedRowNumber]) => [
      reviewId,
      { selected_row_number: Number(selectedRowNumber) }
    ])
  ),
  ...Object.fromEntries(Object.entries(semanticResolutions))
})

const startPolling = () => {
  stopPolling()
  pollTimer = setTimeout(async () => {
    pollTimer = null
    await refreshCurrent()
    if (currentImport.value && ACTIVE_STATUSES.has(currentImport.value.status)) startPolling()
  }, 2500)
}

const stopPolling = () => {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

const close = () => emit('update:open', false)
const issueKey = (item) => `${item.code}-${item.row_number || ''}-${item.message}`
const formatDate = (value) => (value ? new Date(value).toLocaleString() : '-')
const estimateProgress = (status) =>
  ({ UPLOADED: 2, PARSING: 5, VALIDATING: 10, IMPORTING: 25, PROJECTING_NEO4J: 45, PROJECTING_MILVUS: 70, RECONCILING: 90, SUCCEEDED: 100, ROLLING_BACK: 55 }[status] || 0)
const getStatusMeta = (status) => {
  const [label, color] = STATUS_META[status] || [status || '未知', 'default']
  return { label, color }
}

onUnmounted(stopPolling)
</script>

<style scoped lang="less">
.import-notice,
.report-alert {
  margin-bottom: 16px;
}

.file-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

:deep(.file-picker) {
  min-height: 138px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px;
  border: 1px dashed var(--gray-250);
  border-radius: 8px;
  background: var(--gray-25);
  color: var(--gray-600);
  cursor: pointer;

  &:hover,
  &.selected {
    color: var(--main-color);
    border-color: var(--main-color);
    background: var(--main-10);
  }

  input {
    display: none;
  }

  .file-title {
    font-weight: 600;
    color: var(--gray-900);
  }

  .file-name {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-hint {
    font-size: 12px;
    color: var(--gray-500);
  }
}

.primary-action,
.report-actions .ant-btn,
.history-toolbar .ant-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.report-card {
  margin-top: 20px;
  padding: 18px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);
}

.report-header,
.history-toolbar,
.conflict-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.report-header {
  margin-bottom: 14px;
}

.report-title {
  font-weight: 600;
  color: var(--gray-1000);
}

.report-id,
.more-hint,
.conflict-help,
.registry-id,
.history-toolbar {
  color: var(--gray-500);
  font-size: 12px;
}

.report-id {
  font-family: monospace;
}

.count-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}

.count-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 10px 4px;
  border-radius: 6px;
  background: var(--gray-50);
}

.count-value {
  font-size: 18px;
  font-weight: 600;
  color: var(--gray-1000);
}

.count-label {
  font-size: 11px;
  color: var(--gray-500);
}

.issue-list,
.conflict-section,
.semantic-section,
.review-disclosure {
  margin-top: 16px;
}

.issue-title {
  margin-bottom: 8px;
  font-weight: 600;
  color: var(--color-warning-600);

  &.error {
    color: var(--color-error-600);
  }

  &.safe {
    color: var(--color-success-600);
  }
}

.issue-row {
  display: grid;
  grid-template-columns: 210px 1fr auto;
  gap: 12px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--gray-100);
  font-size: 12px;
}

.conflict-section {
  max-height: 340px;
  overflow-y: auto;
  padding-right: 4px;
}

.conflict-card {
  padding: 12px;
  margin-bottom: 8px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-25);

  :deep(.ant-radio-group) {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 8px;
  }
}

.conflict-heading {
  font-size: 12px;
  font-weight: 600;
}

.review-disclosure,
.split-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-25);

  summary {
    padding: 11px 12px;
    color: var(--gray-900);
    font-weight: 600;
    cursor: pointer;
  }
}

.case-review-row {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) auto auto;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  border-top: 1px solid var(--gray-100);

  > div:first-child {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
}

.case-aliases,
.registry-links,
.route-row span,
.route-row small,
.split-card summary span {
  color: var(--gray-500);
  font-size: 12px;
}

.registry-links {
  display: flex;
  gap: 8px;
}

.semantic-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.split-card {
  margin-bottom: 8px;

  summary {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }
}

.entity-preview-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-top: 1px solid var(--gray-100);
}

.entity-preview {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;

  > span {
    font-weight: 600;
  }

  code {
    overflow: hidden;
    color: var(--gray-500);
    font-size: 11px;
    text-overflow: ellipsis;
  }
}

.gene-preview {
  border-left: 3px solid var(--main-color);
}

.allele-preview {
  border-left: 3px solid var(--color-warning-500);
}

.allele-arrow {
  color: var(--gray-600);
  font-size: 12px;
  font-weight: 600;
}

.route-table {
  max-height: 250px;
  overflow-y: auto;
  border-top: 1px solid var(--gray-100);
}

.route-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  border-bottom: 1px solid var(--gray-100);

  > div {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
  }

  > label {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--gray-600);
    font-size: 12px;
  }
}

.info-disclosure {
  .issue-row:last-child {
    border-bottom: 0;
  }
}

.report-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}

.history-toolbar {
  margin-bottom: 12px;
}

@media (max-width: 760px) {
  .file-grid,
  .count-grid {
    grid-template-columns: 1fr 1fr;
  }

  .issue-row {
    grid-template-columns: 1fr;
  }

  .case-review-row,
  .entity-preview-grid {
    grid-template-columns: 1fr;
  }

  .allele-arrow {
    transform: rotate(90deg);
    justify-self: center;
  }

  .route-row {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
