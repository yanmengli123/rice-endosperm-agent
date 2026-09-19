<template>
  <div class="graph-section" v-if="isGraphSupported">
    <div class="graph-container-compact">
      <div v-if="!isGraphSupported" class="graph-disabled">
        <div class="disabled-content">
          <h4>知识图谱不可用</h4>
          <p>当前知识库类型 "{{ kbTypeLabel }}" 不支持知识图谱功能。</p>
          <p>只有 Milvus 类型的知识库支持知识图谱。</p>
        </div>
      </div>
      <div v-else class="graph-wrapper">
        <GraphCanvas
          ref="graphRef"
          :graph-data="graph.graphData"
          :total-entities="graphTotalEntities"
          :total-relationships="graphTotalRelationships"
          :truncated="graphTruncated"
          :display-limit="subgraphParams.maxNodes"
          :limit-editable="!subgraphParams.fullGraph"
          @change-display-limit="onDisplayLimitChange"
          @node-click="graph.handleNodeClick"
          @edge-click="graph.handleEdgeClick"
          @canvas-click="graph.handleCanvasClick"
        >
          <template #top>
            <div class="compact-actions">
              <div class="actions-left">
                <a-input
                  v-model:value="searchInput"
                  placeholder="搜索实体"
                  style="width: 240px"
                  @keydown.enter="onSearch"
                  allow-clear
                >
                  <template #suffix>
                    <component
                      :is="graph.fetching ? Loader2 : Search"
                      :size="14"
                      class="search-suffix-icon"
                      @click="onSearch"
                    />
                  </template>
                </a-input>
                <a-button class="action-btn" @click="loadGraph" title="刷新">
                  <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
                </a-button>
              </div>
              <div class="actions-right">
                <a-button
                  v-if="isMilvus"
                  class="action-btn"
                  @click="openGraphImport"
                  title="导入节点 CSV、关系 CSV 与 Cypher 说明"
                >
                  <FileUp :size="16" />
                </a-button>
                <a-dropdown v-if="isMilvus" :trigger="['click']" placement="bottomRight">
                  <a-button
                    class="action-btn"
                    :loading="exportingGraph"
                    title="导出图谱数据（规范层 / Neo4j 投影）"
                    @click.prevent
                  >
                    <Download :size="16" />
                  </a-button>
                  <template #overlay>
                    <a-menu @click="onExportMenuClick">
                      <a-menu-item
                        key="roundtrip"
                        title="严格符合导入契约的节点/关系 CSV 与清单，可直接重新导入"
                      >
                        标准往返包（CSV + 清单）
                      </a-menu-item>
                      <a-menu-item
                        key="evidence"
                        title="实体 / 三元组 / 证据明细 / 原文引文四类工作表，供科研审阅"
                      >
                        证据明细（Excel）
                      </a-menu-item>
                      <a-menu-item
                        key="projection"
                        title="Neo4j 投影的全部实体节点、块节点与全部关系，附逐条原文引文、段落全文与人工决策（JSONL + 清单）"
                      >
                        Neo4j 投影全量（含原文证据与段落）
                      </a-menu-item>
                      <a-menu-item
                        key="projection-quotes"
                        title="含逐条引文与校验态，但不含段落全文：体积更小，引文来源仍可核对"
                      >
                        Neo4j 投影全量（含引文，不含段落）
                      </a-menu-item>
                      <a-menu-item
                        key="projection-structure"
                        title="仅节点、关系与对账清单，不含证据成员（体积最小）"
                      >
                        Neo4j 投影（仅结构，轻量）
                      </a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>
                <a-button
                  v-if="isMilvus"
                  class="action-btn index-action-btn"
                  :class="{ 'has-index-label': hasPendingGraphChunks }"
                  @click="toggleBuildPanel"
                  :title="graphIndexButtonTitle"
                  :aria-label="graphIndexButtonTitle"
                >
                  <Database :size="16" />
                  <span v-if="isBuildActive" class="index-status-label"
                    >索引中 · {{ pendingGraphChunks }} 待处理</span
                  >
                  <span v-else-if="hasPendingGraphChunks" class="index-status-label"
                    >{{ pendingGraphChunks }} 待索引</span
                  >
                  <span
                    v-if="graphIndexDotStatus"
                    class="status-dot"
                    :class="`status-dot--${graphIndexDotStatus}`"
                  ></span>
                </a-button>
                <a-button
                  v-if="isMilvus"
                  class="action-btn"
                  @click="showReviewQueue = true"
                  title="审核队列：验证 / 拒绝 / 批量处理 LLM 抽取的候选关系"
                >
                  <ClipboardCheck :size="16" />
                </a-button>
                <a-button
                  v-if="isMilvus"
                  class="action-btn"
                  @click="showGateReviewQueue = true"
                  title="门禁送审队列：G7/G9 送审候选的人工裁决"
                >
                  <ScanText :size="16" />
                </a-button>
                <a-button
                  v-if="isMilvus"
                  class="action-btn"
                  @click="showConflictQueue = true"
                  title="冲突队列：极性矛盾与定义口径不一的登记与处置"
                >
                  <Network :size="16" />
                </a-button>
                <a-button class="action-btn" @click="toggleSettingsPanel" title="设置">
                  <Settings :size="16" />
                </a-button>
              </div>
            </div>
          </template>
        </GraphCanvas>
        <ResourceEmptyState
          v-if="showGraphConfigEmpty"
          class="graph-empty-state"
          title="暂无知识图谱"
          description="配置抽取器后，才能从当前知识库构建实体与关系。"
          :icon="Network"
          full-height
        >
          <template #actions>
            <a-button type="primary" class="lucide-icon-btn" @click="openGraphConfig">
              <Settings :size="16" />
              配置抽取器
            </a-button>
          </template>
        </ResourceEmptyState>
        <ResourceEmptyState
          v-else-if="showGraphDataEmpty"
          class="graph-empty-state"
          :title="graphDataEmptyTitle"
          :description="graphDataEmptyDescription"
          :icon="Network"
          full-height
        >
          <template #actions>
            <a-button v-if="searchInput.trim()" class="lucide-icon-btn" @click="clearGraphSearch">
              <Search :size="16" />
              清空搜索
            </a-button>
            <a-button
              v-else-if="hasPendingGraphChunks && !isBuildActive"
              type="primary"
              class="lucide-icon-btn"
              @click="startGraphBuild"
            >
              <Database :size="16" />
              开始索引
            </a-button>
            <a-button v-else class="lucide-icon-btn" @click="loadGraph">
              <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
              刷新图谱
            </a-button>
          </template>
        </ResourceEmptyState>

        <!-- 详情浮动卡片 -->
        <GraphDetailPanel
          :visible="graph.showDetailDrawer"
          :item="graph.selectedItem"
          :type="graph.selectedItemType"
          :kb-id="kbId"
          @close="graph.handleCanvasClick"
          @reviewed="loadGraph"
        />

        <!-- 设置浮动面板 -->
        <transition name="slide-fade">
          <div v-if="showSettings" class="floating-panel settings-panel">
            <div class="panel-header">
              <span class="panel-title">图谱设置</span>
            </div>
            <div class="panel-body">
              <a-form layout="vertical">
                <div v-if="graphTotalEntities != null" class="graph-total-hint">
                  本库共 {{ graphTotalEntities }} 实体 / {{ graphTotalRelationships ?? '-' }} 关系
                </div>
                <a-form-item label="全图模式">
                  <div class="full-graph-row">
                    <a-switch
                      v-model:checked="settingsForm.fullGraph"
                      :disabled="graphSettingsSaving"
                    />
                    <span class="full-graph-hint"
                      >加载全库（口径与规范层一致），忽略搜索、深度与上限</span
                    >
                  </div>
                </a-form-item>
                <a-form-item label="最大节点数 (limit)">
                  <a-input-number
                    v-model:value="settingsForm.maxNodes"
                    :min="10"
                    :max="1000"
                    :step="10"
                    :disabled="graphSettingsSaving || settingsForm.fullGraph"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="搜索深度 (depth)">
                  <a-input-number
                    v-model:value="settingsForm.maxDepth"
                    :min="1"
                    :max="5"
                    :step="1"
                    :disabled="graphSettingsSaving || settingsForm.fullGraph"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="排除 Chunk 节点">
                  <a-switch
                    v-model:checked="settingsForm.excludeChunk"
                    :disabled="graphSettingsSaving"
                  />
                </a-form-item>
                <a-form-item label="审核策略">
                  <a-radio-group
                    v-model:value="settingsForm.reviewPolicy"
                    :disabled="graphSettingsSaving"
                    size="small"
                  >
                    <a-radio-button value="candidates_visible">候选可见</a-radio-button>
                    <a-radio-button value="approved_only">仅已验证</a-radio-button>
                  </a-radio-group>
                  <div class="full-graph-hint">
                    「仅已验证」下主图与 Graph-RAG 检索只含人工验证与规范层的关系
                  </div>
                </a-form-item>
                <a-form-item>
                  <a-button
                    type="primary"
                    :loading="graphSettingsSaving"
                    @click="applySettings"
                    style="width: 100%"
                  >
                    应用
                  </a-button>
                </a-form-item>
              </a-form>
            </div>
          </div>
        </transition>

        <!-- 索引管理浮动面板 -->
        <transition name="slide-fade">
          <div v-if="isMilvus && showBuildPanel" class="floating-panel build-panel">
            <div class="panel-header">
              <span class="panel-title">索引管理</span>
              <a-button
                size="small"
                type="text"
                :disabled="graphBuildLoading"
                @click="loadGraphBuildStatus"
                class="panel-refresh-btn"
              >
                <RefreshCw :size="14" :class="{ spin: graphBuildLoading }" />
              </a-button>
            </div>
            <div class="panel-body">
              <div class="status-row">
                <span class="status-label">状态</span>
                <a-tag v-if="isBuildActive" color="blue" size="small">构建中</a-tag>
                <a-tag v-else-if="isBuildFailed" color="red" size="small">构建失败</a-tag>
                <a-tag v-else-if="graphBuildStatus?.locked" color="green" size="small"
                  >已配置</a-tag
                >
                <a-tag v-else color="orange" size="small">未配置</a-tag>
              </div>
              <a-progress
                v-if="isBuildActive"
                :percent="graphBuildStatus?.build_task_progress ?? 0"
                :stroke-color="{ '0%': '#108ee9', '100%': '#87d068' }"
                size="small"
                style="margin-bottom: 10px"
              />
              <a-alert
                v-if="isBuildFailed"
                class="build-error-alert"
                type="error"
                show-icon
                :message="graphBuildStatus?.build_task_message || '图谱索引未全部完成'"
                :description="
                  graphBuildStatus?.build_task_error || '请检查模型服务后重试待索引 Chunk。'
                "
              />
              <div v-if="graphBuildModelSpec" class="status-row">
                <span class="status-label">抽取模型</span>
                <span class="status-model" :title="graphBuildModelSpec">{{
                  graphBuildModelSpec
                }}</span>
              </div>
              <div class="stats-grid">
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.total_chunks ?? '-' }}</span>
                  <span class="stat-label">总 Chunk</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.pending_chunks ?? '-' }}</span>
                  <span class="stat-label">待构建</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.indexed_chunks ?? '-' }}</span>
                  <span class="stat-label">已构建</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.entity_count ?? '-' }}</span>
                  <span class="stat-label">实体</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.relationship_count ?? '-' }}</span>
                  <span class="stat-label">关系</span>
                </div>
              </div>
              <div class="build-actions">
                <a-button
                  v-if="!graphBuildStatus?.locked"
                  type="primary"
                  block
                  @click="openGraphConfig"
                >
                  配置抽取器
                </a-button>
                <a-button v-else-if="isBuildActive" type="primary" block disabled>
                  构建中 {{ graphBuildStatus?.build_task_progress ?? 0 }}%
                </a-button>
                <a-button
                  v-else-if="isBuildFailed"
                  type="primary"
                  block
                  :disabled="!graphBuildStatus?.pending_chunks"
                  @click="startGraphBuild"
                >
                  重试索引
                </a-button>
                <a-button
                  v-else
                  type="primary"
                  block
                  :disabled="!graphBuildStatus?.pending_chunks"
                  @click="startGraphBuild"
                >
                  开始索引
                </a-button>
                <div class="actions-secondary">
                  <a-button
                    v-if="graphBuildStatus?.locked && !isBuildActive"
                    size="small"
                    type="text"
                    @click="openGraphConfig"
                  >
                    修改配置
                  </a-button>
                  <a-button
                    size="small"
                    type="text"
                    danger
                    v-if="graphBuildStatus?.locked && !isBuildActive"
                    @click="confirmResetGraph"
                    >重置</a-button
                  >
                </div>
              </div>
            </div>
          </div>
        </transition>
      </div>
    </div>

    <a-modal
      v-model:open="showGraphConfig"
      :title="graphConfigTitle"
      width="640px"
      @ok="configureGraphBuild"
    >
      <a-form layout="vertical">
        <a-alert
          v-if="isEditingGraphConfig"
          class="config-warning"
          type="warning"
          show-icon
          message="修改配置仅影响后续构建；已构建的图谱不会自动重算，如需一致请重置后重新抽取。抽取器类型创建后不可修改。"
        />
        <a-form-item label="抽取器类型">
          <div class="extractor-type-cards" role="radiogroup" aria-label="抽取器类型">
            <div
              v-for="option in extractorTypeOptions"
              :key="option.value"
              class="extractor-type-card"
              :class="{
                active: graphConfigForm.extractor_type === option.value,
                disabled: isEditingGraphConfig || option.disabled
              }"
              role="radio"
              :aria-checked="graphConfigForm.extractor_type === option.value"
              :aria-disabled="isEditingGraphConfig || option.disabled"
              :tabindex="isEditingGraphConfig || option.disabled ? -1 : 0"
              @click="selectExtractorType(option)"
              @keydown.enter.prevent="selectExtractorType(option)"
              @keydown.space.prevent="selectExtractorType(option)"
            >
              <div class="card-header">
                <component :is="option.icon" class="type-icon" />
                <span class="type-title">{{ option.label }}</span>
              </div>
              <div class="card-description">{{ option.description }}</div>
              <div v-if="option.helper" class="card-helper" :class="{ warning: option.disabled }">
                {{ option.helper }}
              </div>
            </div>
          </div>
        </a-form-item>
        <a-form-item label="模型">
          <ModelSelectorComponent
            :model_spec="graphConfigForm.model_spec"
            placeholder="选择抽取模型"
            @select-model="(spec) => (graphConfigForm.model_spec = spec)"
          />
        </a-form-item>
        <a-form-item v-if="isScientificExtractor" label="抽取约束">
          <a-alert
            type="info"
            show-icon
            message="闭集词表科研抽取：实体类型与关系谓词固定为托管图谱白名单（16 类实体 / 21 种关系），按句窗抽取并经逐字校验门；不接受自定义 Schema。构建结果中的 extraction_stats 会给出候选数、拒绝分布与幻觉率。"
          />
        </a-form-item>
        <a-form-item v-else label="Schema">
          <a-textarea
            v-model:value="graphConfigForm.schema"
            :rows="6"
            placeholder="描述实体类型、关系类型和属性约束。后端会把 Schema 拼接到固定抽取 Prompt 中。"
          />
        </a-form-item>
        <div class="form-grid two-columns">
          <a-form-item label="并发队列数">
            <a-input-number
              v-model:value="graphConfigForm.concurrency_count"
              :min="1"
              :max="1000"
              :step="1"
              style="width: 100%"
            />
          </a-form-item>
          <a-form-item label="模型参数 JSON">
            <a-input
              v-model:value="graphConfigForm.model_params_text"
              placeholder='例如 {"temperature":0.1}'
            />
          </a-form-item>
        </div>
      </a-form>
    </a-modal>
    <GraphImportModal
      v-model:open="showGraphImport"
      :kb-id="kbId"
      @imported="handleGraphImported"
    />
    <GraphReviewQueue v-model:open="showReviewQueue" :kb-id="kbId" @reviewed="loadGraph" />
    <GateReviewQueue v-model:open="showGateReviewQueue" :kb-id="kbId" @reviewed="loadGraph" />
    <ConflictQueue v-model:open="showConflictQueue" :kb-id="kbId" @reviewed="loadGraph" />
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onUnmounted, reactive } from 'vue'
import { useDatabaseStore } from '@/stores/database'
import { useTaskerStore } from '@/stores/tasker'
import { useConfigStore } from '@/stores/config'
import {
  RefreshCw,
  Settings,
  Search,
  Loader2,
  Database,
  FileUp,
  Download,
  Network,
  BrainCircuit,
  ScanText,
  ClipboardCheck
} from '@lucide/vue'
import GraphCanvas from '@/components/GraphCanvas.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'
import GraphImportModal from '@/components/GraphImportModal.vue'
import GraphReviewQueue from '@/components/GraphReviewQueue.vue'
import GateReviewQueue from '@/components/GateReviewQueue.vue'
import ConflictQueue from '@/components/ConflictQueue.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import { getKbTypeLabel } from '@/utils/kb_utils'
import { unifiedApi } from '@/apis/graph_api'
import { graphBuildApi, graphExportApi } from '@/apis/knowledge_api'
import { Modal, message } from 'ant-design-vue'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'
import { useGraph } from '@/composables/useGraph'

const GRAPH_BUILD_TASK_TYPE = 'knowledge_graph_index'
const MILVUS_KB_TYPE = 'milvus'
const GRAPH_SUPPORTED_KB_TYPES = new Set([MILVUS_KB_TYPE])
const DEFAULT_GRAPH_VIEW_SETTINGS = Object.freeze({
  maxNodes: 100,
  maxDepth: 2,
  excludeChunk: true,
  fullGraph: false,
  reviewPolicy: 'candidates_visible'
})

const props = defineProps({
  active: {
    type: Boolean,
    default: false
  }
})

const store = useDatabaseStore()
const taskerStore = useTaskerStore()
const configStore = useConfigStore()

const kbId = computed(() => store.kbId)
const kbType = computed(() => store.database.kb_type)
const kbTypeLabel = computed(() => getKbTypeLabel(kbType.value || 'milvus'))
const isMilvus = computed(() => kbType.value?.toLowerCase() === MILVUS_KB_TYPE)

const graphRef = ref(null)
const showSettings = ref(false)
const showBuildPanel = ref(false)
const subgraphParams = reactive({ ...DEFAULT_GRAPH_VIEW_SETTINGS })
const settingsForm = reactive({ ...DEFAULT_GRAPH_VIEW_SETTINGS })
const graphSettingsLoadedKbId = ref('')
const graphSettingsLoading = ref(false)
const graphSettingsSaving = ref(false)
const searchInput = ref('')
const graphBuildStatus = ref(null)
const graphBuildLoading = ref(false)
const showGraphConfig = ref(false)
const showGraphImport = ref(false)
const showReviewQueue = ref(false)
const showGateReviewQueue = ref(false)
const showConflictQueue = ref(false)
let buildStatusPollTimer = null
let graphSettingsRequestSeq = 0
let graphSettingsLoadPromise = null
let graphSettingsLoadKbId = ''

const extractorTypeOptions = [
  {
    value: 'llm',
    label: 'LLM',
    description: '使用大模型按 Schema 抽取实体和关系',
    helper: '通用开放 Schema，适合非科研语料',
    icon: BrainCircuit,
    disabled: false
  },
  {
    value: 'llm_scientific',
    label: '科研闭集',
    description: '闭集词表 + 句窗抽取 + 逐字校验门，产出可度量的科研三元组',
    helper: '推荐用于人工整理的文献结果段 Markdown',
    icon: ScanText,
    disabled: false
  }
]

const isBuildActive = computed(() => {
  const s = graphBuildStatus.value?.build_task_status
  return s === 'pending' || s === 'running'
})

const isBuildFailed = computed(() => {
  return graphBuildStatus.value?.build_task_status === 'failed'
})

const graphBuildModelSpec = computed(() => {
  return graphBuildStatus.value?.config?.extractor_options?.model_spec || ''
})

const pendingGraphChunks = computed(() => {
  return Number(graphBuildStatus.value?.pending_chunks ?? 0)
})

const hasPendingGraphChunks = computed(() => pendingGraphChunks.value > 0)

const isGraphIndexComplete = computed(() => {
  return (
    Boolean(graphBuildStatus.value?.locked) &&
    !isBuildActive.value &&
    pendingGraphChunks.value === 0
  )
})

const graphIndexDotStatus = computed(() => {
  if (isBuildActive.value) return 'active'
  if (hasPendingGraphChunks.value) return 'pending'
  if (isGraphIndexComplete.value) return 'complete'
  return ''
})

const graphIndexButtonTitle = computed(() => {
  if (isBuildActive.value) return `索引管理，索引中，${pendingGraphChunks.value} 待处理`
  if (hasPendingGraphChunks.value) return `索引管理，${pendingGraphChunks.value} 待索引`
  if (isGraphIndexComplete.value) return '索引管理，已全部索引'
  return '索引管理'
})

const toggleBuildPanel = () => {
  showBuildPanel.value = !showBuildPanel.value
  showSettings.value = false
}

const toggleSettingsPanel = async () => {
  const opening = !showSettings.value
  showSettings.value = opening
  showBuildPanel.value = false
  if (opening) {
    await loadGraphSettings()
    Object.assign(settingsForm, subgraphParams)
  }
}

const openGraphImport = () => {
  showBuildPanel.value = false
  showSettings.value = false
  showGraphImport.value = true
}

const handleGraphImported = async () => {
  await Promise.all([loadGraphBuildStatus(), loadGraph()])
}

const exportingGraph = ref(false)

const parseExportFilename = (contentDisposition) => {
  if (!contentDisposition) return ''
  const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      return utf8Match[1]
    }
  }
  const asciiMatch = contentDisposition.match(/filename="?([^";]+)"?/i)
  return asciiMatch ? asciiMatch[1] : ''
}

const onExportMenuClick = ({ key }) => {
  exportGraph(key)
}

const EXPORT_MENU_ITEMS = {
  roundtrip: { variant: 'roundtrip', label: '标准往返包' },
  evidence: { variant: 'evidence', label: '证据明细' },
  projection: {
    variant: 'projection',
    options: { includeEvidence: true, includeChunkText: true },
    label: 'Neo4j 投影全量（含原文证据与段落）'
  },
  'projection-quotes': {
    variant: 'projection',
    options: { includeEvidence: true, includeChunkText: false },
    label: 'Neo4j 投影全量（含引文，不含段落）'
  },
  'projection-structure': {
    variant: 'projection',
    options: { includeEvidence: false, includeChunkText: false },
    label: 'Neo4j 投影（仅结构，轻量）'
  }
}

// X-Export-Evidence-Summary 是 ASCII 键值（HTTP 头必须 latin-1），这里翻成中文提示
const EVIDENCE_SUMMARY_LABELS = {
  evidence: '引文',
  ok: '逐字命中',
  degraded: '与原文不一致',
  missing: '无引文',
  unverifiable: '不可比对',
  chunks: '段落',
  decisions: '决策'
}

const formatEvidenceSummary = (raw) => {
  if (!raw) return ''
  return String(raw)
    .split(';')
    .map((token) => token.trim())
    .filter(Boolean)
    .map((token) => {
      const [key, value] = token.split('=').map((item) => (item || '').trim())
      if (key === 'complete') return value === 'true' ? '证据覆盖完整' : '证据覆盖不完整'
      const label = EVIDENCE_SUMMARY_LABELS[key]
      return label && value ? `${label} ${value}` : ''
    })
    .filter(Boolean)
    .join(' · ')
}

const exportGraph = async (menuKey) => {
  const item = EXPORT_MENU_ITEMS[menuKey]
  if (!item || !kbId.value || exportingGraph.value) return
  exportingGraph.value = true
  try {
    const response = await graphExportApi.exportGraph(kbId.value, item.variant, item.options)
    const blob = await response.blob()
    const contentDisposition =
      response.headers.get('Content-Disposition') || response.headers.get('content-disposition')
    const suffix = item.variant === 'evidence' ? 'xlsx' : 'zip'
    const filename =
      parseExportFilename(contentDisposition) || `graph-${item.variant}-${kbId.value}.${suffix}`
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    const summary = formatEvidenceSummary(
      response.headers.get('X-Export-Evidence-Summary') ||
        response.headers.get('x-export-evidence-summary')
    )
    message.success(`${item.label}导出成功${summary ? `（${summary}）` : ''}`)
  } catch (error) {
    console.error('图谱导出失败:', error)
    message.error(`图谱导出失败: ${error.message || '未知错误'}`)
  } finally {
    exportingGraph.value = false
  }
}

const isEditingGraphConfig = computed(() => Boolean(graphBuildStatus.value?.locked))

const graphConfigTitle = computed(() =>
  isEditingGraphConfig.value ? '修改图谱抽取配置' : '配置图谱抽取器'
)

const stopBuildStatusPoll = () => {
  if (buildStatusPollTimer) {
    clearInterval(buildStatusPollTimer)
    buildStatusPollTimer = null
  }
}

const startBuildStatusPoll = () => {
  stopBuildStatusPoll()
  buildStatusPollTimer = setInterval(() => {
    loadGraphBuildStatus()
  }, 5000)
}

const shouldPollBuildStatus = computed(() => props.active && isBuildActive.value)

watch(
  shouldPollBuildStatus,
  (active) => {
    if (active) {
      startBuildStatusPoll()
    } else {
      stopBuildStatusPoll()
    }
  },
  { immediate: true }
)
const graphConfigForm = reactive({
  extractor_type: 'llm',
  model_spec: '',
  schema: '',
  concurrency_count: 50,
  model_params_text: ''
})
const isScientificExtractor = computed(() => graphConfigForm.extractor_type === 'llm_scientific')

const graph = reactive(useGraph(graphRef))
const graphLoaded = ref(false)

// 计算属性：是否支持知识图谱
const isGraphSupported = computed(() => GRAPH_SUPPORTED_KB_TYPES.has(kbType.value?.toLowerCase()))
const hasGraphNodes = computed(() => graph.graphData.nodes.length > 0)
const showGraphConfigEmpty = computed(
  () =>
    isMilvus.value &&
    !graphBuildStatus.value?.locked &&
    !graphBuildLoading.value &&
    graphLoaded.value &&
    !hasGraphNodes.value
)
const showGraphDataEmpty = computed(
  () =>
    isMilvus.value &&
    Boolean(graphBuildStatus.value?.locked) &&
    graphLoaded.value &&
    !graph.fetching &&
    !hasGraphNodes.value
)
const graphDataEmptyTitle = computed(() =>
  searchInput.value.trim() ? '未找到匹配实体' : '暂无知识图谱'
)
const graphDataEmptyDescription = computed(() => {
  if (searchInput.value.trim()) return '换个关键词或调整图谱设置后再搜索。'
  if (isBuildActive.value) return '图谱索引正在运行，完成后会展示实体与关系。'
  if (hasPendingGraphChunks.value) return '当前还有待索引 Chunk，完成索引后会展示实体与关系。'
  return '当前知识库还没有可展示的实体与关系。'
})

let pendingLoadTimer = null
let graphStatusRequestSeq = 0
let graphLoadRequestSeq = 0

const getErrorDetail = (e, fallback) => {
  return e?.response?.data?.detail || e?.response?.data?.message || e?.message || fallback
}

const normalizeGraphViewSettings = (value = {}) => ({
  maxNodes: Math.min(1000, Math.max(10, Number(value.max_nodes) || 100)),
  maxDepth: Math.min(5, Math.max(1, Number(value.max_depth) || 2)),
  excludeChunk: typeof value.exclude_chunk === 'boolean' ? value.exclude_chunk : true,
  reviewPolicy: value.review_policy === 'approved_only' ? 'approved_only' : 'candidates_visible'
})

// 全库存量（PG 规范层口径），用于左下角「可见/全量」复合显示
const graphTotalEntities = computed(() => {
  const value = Number(graphBuildStatus.value?.entity_count)
  return Number.isFinite(value) ? value : null
})

const graphTotalRelationships = computed(() => {
  const value = Number(graphBuildStatus.value?.relationship_count)
  return Number.isFinite(value) ? value : null
})

// 当前画布是否被显示上限/安全上限截断（普通模式：节点数触达 maxNodes；全图模式：后端 truncated 标志）
const graphTruncated = ref(false)

const resetGraphViewSettings = () => {
  Object.assign(subgraphParams, DEFAULT_GRAPH_VIEW_SETTINGS)
  Object.assign(settingsForm, DEFAULT_GRAPH_VIEW_SETTINGS)
}

const loadGraphSettings = async (force = false) => {
  const currentDatabaseId = kbId.value
  if (!currentDatabaseId || !isGraphSupported.value) return false
  if (!force && graphSettingsLoadedKbId.value === currentDatabaseId) return true
  if (!force && graphSettingsLoadPromise && graphSettingsLoadKbId === currentDatabaseId) {
    return graphSettingsLoadPromise
  }

  const requestSeq = ++graphSettingsRequestSeq
  graphSettingsLoading.value = true
  graphSettingsLoadKbId = currentDatabaseId
  const request = (async () => {
    try {
      const response = await unifiedApi.getViewSettings(currentDatabaseId)
      if (requestSeq !== graphSettingsRequestSeq || currentDatabaseId !== kbId.value) return false
      const settings = normalizeGraphViewSettings(response?.data)
      Object.assign(subgraphParams, settings)
      Object.assign(settingsForm, settings)
      graphSettingsLoadedKbId.value = currentDatabaseId
      return true
    } catch (e) {
      console.error('Failed to load graph view settings:', e)
      if (requestSeq === graphSettingsRequestSeq && currentDatabaseId === kbId.value) {
        resetGraphViewSettings()
        graphSettingsLoadedKbId.value = currentDatabaseId
        message.warning('读取图谱设置失败，已暂时使用默认值')
      }
      return false
    } finally {
      if (requestSeq === graphSettingsRequestSeq) {
        graphSettingsLoading.value = false
      }
    }
  })()

  graphSettingsLoadPromise = request
  try {
    return await request
  } finally {
    if (graphSettingsLoadPromise === request) {
      graphSettingsLoadPromise = null
      graphSettingsLoadKbId = ''
    }
  }
}

const loadGraphBuildStatus = async () => {
  if (!kbId.value || !isMilvus.value) return
  const requestSeq = ++graphStatusRequestSeq
  const currentDatabaseId = kbId.value
  graphBuildLoading.value = true
  try {
    const status = await graphBuildApi.getStatus(currentDatabaseId)
    if (requestSeq === graphStatusRequestSeq && currentDatabaseId === kbId.value) {
      graphBuildStatus.value = status
    }
  } catch (e) {
    console.error('Failed to load graph build status:', e)
    message.error('加载图谱构建状态失败')
  } finally {
    if (requestSeq === graphStatusRequestSeq) {
      graphBuildLoading.value = false
    }
  }
}

const parseModelParams = () => {
  const text = graphConfigForm.model_params_text.trim()
  if (!text) return {}
  let params
  try {
    params = JSON.parse(text)
  } catch {
    throw new Error('模型参数必须是合法 JSON 对象')
  }
  if (!params || Array.isArray(params) || typeof params !== 'object') {
    throw new Error('模型参数必须是 JSON 对象')
  }
  return params
}

const fillGraphConfigForm = () => {
  const config = graphBuildStatus.value?.config
  const options = config?.extractor_options || {}
  graphConfigForm.extractor_type = config?.extractor_type || 'llm'
  graphConfigForm.model_spec = options.model_spec || configStore.config?.default_model || ''
  graphConfigForm.schema = options.schema || ''
  graphConfigForm.concurrency_count = Number(options.concurrency_count || 50)
  graphConfigForm.model_params_text = options.model_params
    ? JSON.stringify(options.model_params)
    : ''
}

const openGraphConfig = () => {
  fillGraphConfigForm()
  showGraphConfig.value = true
}

const selectExtractorType = (option) => {
  if (isEditingGraphConfig.value || option.disabled) return
  graphConfigForm.extractor_type = option.value
}

const buildExtractorOptions = () => {
  const options = {
    model_spec: graphConfigForm.model_spec,
    concurrency_count: graphConfigForm.concurrency_count || 50,
    model_params: parseModelParams()
  }
  // 科研闭集抽取器使用固定词表 Prompt，后端拒绝 schema 字段
  if (!isScientificExtractor.value) {
    options.schema = graphConfigForm.schema.trim()
  }
  return options
}

const configureGraphBuild = async () => {
  try {
    document.activeElement?.blur()
    await nextTick()
    await graphBuildApi.configure(kbId.value, {
      extractor_type: graphConfigForm.extractor_type,
      extractor_options: buildExtractorOptions()
    })
    message.success(isEditingGraphConfig.value ? '图谱抽取配置已更新' : '图谱抽取配置已保存')
    showGraphConfig.value = false
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to configure graph build:', e)
    message.error(getErrorDetail(e, '配置图谱抽取失败'))
  }
}

const startGraphBuild = async () => {
  try {
    const data = await graphBuildApi.startIndex(kbId.value, 20)
    message.success(data.message || '图谱构建任务已提交')
    if (data.task_id) {
      taskerStore.registerQueuedTask({
        task_id: data.task_id,
        name: `图谱构建 (${kbId.value})`,
        task_type: GRAPH_BUILD_TASK_TYPE,
        message: data.message,
        payload: { kb_id: kbId.value, model_spec: graphBuildModelSpec.value }
      })
    }
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to start graph build:', e)
    message.error(getErrorDetail(e, '提交图谱构建任务失败'))
  }
}

const confirmResetGraph = () => {
  Modal.confirm({
    title: '清空并重建图谱',
    content: '将删除该知识库在 Neo4j 中的图谱，重置 Chunk 图谱状态，并清空抽取结果与配置。',
    okText: '确认重置',
    cancelText: '取消',
    onOk: resetGraphBuild
  })
}

const resetGraphBuild = async () => {
  try {
    await graphBuildApi.reset(kbId.value, {
      clear_extraction_result: true,
      clear_config: true
    })
    message.success('图谱构建状态已重置')
    graphLoaded.value = false
    graph.clearGraph()
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to reset graph build:', e)
    message.error(getErrorDetail(e, '重置图谱构建状态失败'))
  }
}

const loadGraph = async () => {
  if (!kbId.value || !isGraphSupported.value) return

  const requestedDatabaseId = kbId.value
  await loadGraphSettings()
  if (requestedDatabaseId !== kbId.value || !isGraphSupported.value) return

  const requestSeq = ++graphLoadRequestSeq
  const currentDatabaseId = kbId.value
  graph.fetching = true
  if (!hasGraphNodes.value) {
    graphLoaded.value = false
  }
  try {
    const requestParams = {
      kb_id: currentDatabaseId,
      node_label: searchInput.value || '*',
      max_nodes: subgraphParams.maxNodes,
      max_depth: subgraphParams.maxDepth,
      exclude_chunk: subgraphParams.excludeChunk
    }
    if (subgraphParams.fullGraph) {
      requestParams.full_graph = true
    }
    const res = await unifiedApi.getSubgraph(requestParams)

    if (
      requestSeq === graphLoadRequestSeq &&
      currentDatabaseId === kbId.value &&
      res.success &&
      res.data
    ) {
      graph.updateGraphData(res.data.nodes, res.data.edges)
      graphTruncated.value = subgraphParams.fullGraph
        ? Boolean(res.data.truncated)
        : (res.data.nodes || []).length >= subgraphParams.maxNodes
      if (graphTruncated.value && subgraphParams.fullGraph) {
        message.warning(
          '全图规模超过安全上限（节点 3000 / 关系 6000），画布已截断；建议改用搜索或类型过滤缩小范围'
        )
      }
    }
  } catch (e) {
    console.error('Failed to load graph:', e)
    message.error('加载图谱失败')
  } finally {
    if (requestSeq === graphLoadRequestSeq) {
      graph.fetching = false
      graphLoaded.value = true
    }
  }
}

const applySettings = async () => {
  if (!kbId.value || graphSettingsSaving.value) return
  const currentDatabaseId = kbId.value
  graphSettingsSaving.value = true
  try {
    const response = await unifiedApi.updateViewSettings(currentDatabaseId, {
      max_nodes: settingsForm.maxNodes,
      max_depth: settingsForm.maxDepth,
      exclude_chunk: settingsForm.excludeChunk,
      review_policy: settingsForm.reviewPolicy
    })
    if (currentDatabaseId !== kbId.value) return
    const settings = normalizeGraphViewSettings(response?.data)
    // fullGraph 是会话级 UI 开关（后端设置不持久化），从表单携带
    Object.assign(subgraphParams, settings, { fullGraph: settingsForm.fullGraph })
    Object.assign(settingsForm, settings, { fullGraph: settingsForm.fullGraph })
    graphSettingsLoadedKbId.value = currentDatabaseId
    showSettings.value = false
    message.success(response?.message || '图谱设置已保存并全局应用')
    await loadGraph()
  } catch (e) {
    console.error('Failed to save graph view settings:', e)
    message.error(getErrorDetail(e, '保存图谱设置失败'))
  } finally {
    graphSettingsSaving.value = false
  }
}

// 画布内联「上限」控件：调整展示数目并持久化（会话内即时生效）
const onDisplayLimitChange = async (value) => {
  const clamped = Math.min(1000, Math.max(10, Math.round(Number(value) || 100)))
  if (clamped === subgraphParams.maxNodes) return
  subgraphParams.maxNodes = clamped
  settingsForm.maxNodes = clamped
  if (!kbId.value) return
  try {
    await unifiedApi.updateViewSettings(kbId.value, {
      max_nodes: clamped,
      max_depth: subgraphParams.maxDepth,
      exclude_chunk: subgraphParams.excludeChunk,
      review_policy: subgraphParams.reviewPolicy
    })
  } catch (e) {
    // 持久化失败不阻塞本地生效，下次进入回退为旧值
    console.warn('保存展示上限设置失败（本地已生效）:', e)
  }
  await loadGraph()
}

const onSearch = () => {
  loadGraph()
}

const clearGraphSearch = () => {
  searchInput.value = ''
  loadGraph()
}

const scheduleGraphLoad = (delay = 200) => {
  if (!props.active || !isGraphSupported.value || !kbId.value) {
    return
  }

  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
  }
  pendingLoadTimer = setTimeout(async () => {
    pendingLoadTimer = null
    await nextTick()
    if (props.active && isGraphSupported.value && kbId.value) {
      await loadGraph()
    }
  }, delay)
}

watch(
  () => props.active,
  (active) => {
    if (active) {
      if (isMilvus.value) {
        loadGraphBuildStatus()
      }
      scheduleGraphLoad()
    }
  },
  { immediate: true }
)

watch(kbId, () => {
  graphStatusRequestSeq += 1
  graphLoadRequestSeq += 1
  graphSettingsRequestSeq += 1
  graphSettingsLoadedKbId.value = ''
  graphSettingsLoadPromise = null
  graphSettingsLoadKbId = ''
  graphSettingsLoading.value = false
  resetGraphViewSettings()
  graphLoaded.value = false
  graph.clearGraph()
  graphBuildStatus.value = null
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  if (isGraphSupported.value) {
    scheduleGraphLoad(300)
  }
})

watch(isGraphSupported, (supported) => {
  if (!supported) {
    graphSettingsRequestSeq += 1
    graphSettingsLoadedKbId.value = ''
    resetGraphViewSettings()
    graphLoaded.value = false
    graph.clearGraph()
    graphBuildStatus.value = null
    return
  }
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  scheduleGraphLoad(200)
})

onUnmounted(() => {
  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
    pendingLoadTimer = null
  }
  stopBuildStatusPoll()
})
</script>

<style scoped lang="less">
.graph-section {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  user-select: none;
}

.graph-container-compact {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

.graph-wrapper {
  height: 100%;
  width: 100%;
  position: relative;
}

.graph-empty-state {
  position: absolute;
  inset: 0;
  z-index: 30;
  pointer-events: none;

  :deep(.resource-empty-state__actions) {
    pointer-events: auto;
  }
}

.compact-actions {
  position: absolute;
  top: 10px;
  left: 10px;
  right: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none; /* Let clicks pass through empty areas */

  .actions-left,
  .actions-right {
    pointer-events: auto; /* Re-enable clicks for buttons/inputs */
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--color-trans-light);
    backdrop-filter: blur(12px);
    padding: 2px;
    border-radius: 8px;
    box-shadow: 0 0 4px 0px var(--shadow-2);
    border: 1px solid var(--gray-100);
  }

  :deep(.ant-input-affix-wrapper) {
    padding: 4px 11px;
    border-radius: 6px;
    border-color: transparent;
    box-shadow: none;
    background: var(--color-trans-light);

    &:hover,
    &:focus,
    &-focused {
      background: var(--main-0);
      border-color: var(--primary-color);
    }

    input {
      background: transparent;
    }
  }

  .action-btn {
    width: 32px;
    height: 32px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--gray-600);
    border-radius: 6px;
    box-shadow: none;
    position: relative;

    &:hover {
      background: var(--shadow-1);
      color: var(--primary-color);
    }
  }

  .index-action-btn {
    gap: 6px;
    overflow: visible;

    &.has-index-label {
      width: auto;
      min-width: 84px;
      padding: 0 22px 0 8px;
      justify-content: flex-start;
    }

    .index-status-label {
      font-size: 12px;
      line-height: 1;
      color: var(--gray-700);
      white-space: nowrap;
    }
  }

  .status-dot {
    position: absolute;
    bottom: 4px;
    right: 4px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    box-shadow: 0 0 0 1px var(--color-trans-light);
  }

  .status-dot--pending {
    background: var(--color-warning-500);
  }

  .status-dot--active {
    background: var(--color-warning-500);
    animation: blink 1.2s ease-in-out infinite;
  }

  .status-dot--complete {
    background: var(--color-success-500);
  }

  .search-suffix-icon {
    cursor: pointer;
  }

  .spin {
    animation: spin 1s linear infinite;
  }
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.2;
  }
}

.graph-disabled {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
}

.disabled-content {
  text-align: center;
  color: var(--gray-400);

  h4 {
    margin-bottom: 8px;
  }
}

.floating-panel {
  position: absolute;
  top: 60px;
  right: 10px;
  width: 300px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }

    .panel-refresh-btn {
      padding: 2px 6px;
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.settings-panel {
  .graph-total-hint {
    margin-bottom: 10px;
    padding: 6px 10px;
    border-radius: 6px;
    background: var(--color-info-50);
    color: var(--color-info-700);
    font-size: 12px;
    line-height: 1.5;
  }

  .full-graph-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .full-graph-hint {
    color: var(--gray-550);
    font-size: 12px;
    line-height: 1.4;
  }
}

.build-panel {
  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;

    .status-label {
      color: var(--gray-600);
      font-size: 12px;
    }

    .status-model {
      max-width: 190px;
      overflow: hidden;
      color: var(--gray-900);
      font-size: 12px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  .build-error-alert {
    margin-bottom: 10px;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }

  .stat-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 6px 4px;
    border-radius: 4px;
    background: var(--gray-50);

    .stat-value {
      font-size: 15px;
      font-weight: 600;
      color: var(--gray-1000);
      line-height: 1.2;
    }

    .stat-label {
      font-size: 11px;
      color: var(--gray-500);
      margin-top: 2px;
    }
  }

  .build-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .actions-secondary {
    display: flex;
    justify-content: space-between;
  }
}

.config-warning {
  margin-bottom: 16px;
}

.extractor-type-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;

  .extractor-type-card {
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    padding: 14px;
    cursor: pointer;
    transition: all 0.2s ease;
    background: var(--gray-0);

    &:hover {
      border-color: var(--main-color);
    }

    &.active {
      border-color: var(--main-color);
      background: var(--main-10);
      box-shadow: 0 0 0 1px var(--main-20);

      .type-icon {
        color: var(--main-color);
      }
    }

    &.disabled {
      cursor: not-allowed;
      opacity: 0.72;
      background: var(--gray-50);

      &:hover {
        border-color: var(--gray-150);
      }
    }

    .card-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }

    .type-icon {
      width: 20px;
      height: 20px;
      color: var(--main-color);
      flex-shrink: 0;
    }

    .type-title {
      font-size: 15px;
      font-weight: 600;
      color: var(--gray-800);
    }

    .card-description {
      font-size: 13px;
      color: var(--gray-600);
      line-height: 1.5;
    }

    .card-helper {
      margin-top: 8px;
      font-size: 12px;
      color: var(--gray-500);

      &.warning {
        color: var(--color-warning-500);
      }
    }
  }
}

.form-grid.two-columns {
  display: grid;
  grid-template-columns: 180px 1fr;
  gap: 12px;

  @media (max-width: 640px) {
    grid-template-columns: 1fr;
  }
}

.slide-fade-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateX(20px);
  opacity: 0;
}
</style>
