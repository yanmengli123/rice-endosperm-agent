<template>
  <div class="graph-wrapper">
    <GraphCanvas
      ref="graphRef"
      :graph-data="graph.graphData"
      :total-entities="graphTotalEntities"
      :total-relationships="graphTotalRelationships"
      :truncated="graphTruncated"
      :display-limit="subgraphParams.maxNodes"
      :limit-editable="!subgraphParams.fullGraph"
      :highlight-keywords="highlightKeywords"
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
              v-if="isLlmGraphAllowed"
              class="action-btn"
              :class="{ 'attention-btn': !graphConfigured }"
              @click="openGraphConfig"
              :title="
                graphConfigured
                  ? '修改图谱抽取配置（抽取器类型、模型、并发）'
                  : '配置抽取器：从当前知识库构建实体与关系'
              "
            >
              <BrainCircuit :size="16" />
            </a-button>
            <a-button
              v-if="isLlmGraphAllowed && graphConfigured"
              class="action-btn"
              :loading="startingIndex"
              :disabled="isBuildActive"
              @click="startGraphBuild"
              title="开始索引：按当前抽取配置处理待构建段落"
            >
              <Database :size="16" />
            </a-button>
            <a-button
              v-if="isLlmGraphAllowed && graphConfigured"
              class="action-btn"
              @click="confirmGraphReset"
              title="清空并重建图谱（重抽所有段落，已固定证据按决策恢复）"
            >
              <RotateCcw :size="16" />
            </a-button>
            <a-button
              v-if="isGraphImportAllowed"
              class="action-btn"
              @click="openGraphImport"
              title="导入节点 CSV、关系 CSV 与 Cypher 说明"
            >
              <FileUp :size="16" />
            </a-button>
            <a-dropdown :trigger="['click']" placement="bottomRight">
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
              v-if="isLlmGraphAllowed"
              class="action-btn"
              @click="showReviewQueue = true"
              title="审核队列：人工批准 / 拒绝 / 批量处理 LLM 抽取的候选关系"
            >
              <ClipboardCheck :size="16" />
            </a-button>
            <a-button
              v-if="isLlmGraphAllowed"
              class="action-btn"
              @click="showGateReviewQueue = true"
              title="门禁送审队列：G7/G9 送审候选的人工裁决"
            >
              <ScanText :size="16" />
            </a-button>
            <a-button
              v-if="isLlmGraphAllowed"
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
      v-if="showGraphDataEmpty"
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
        <template v-else>
          <a-button
            v-if="isLlmGraphAllowed && !graphConfigured"
            type="primary"
            class="lucide-icon-btn"
            @click="openGraphConfig"
          >
            <Settings :size="16" />
            配置抽取器
          </a-button>
          <a-button
            v-else-if="
              isLlmGraphAllowed && graphConfigured && hasPendingGraphChunks && !isBuildActive
            "
            type="primary"
            class="lucide-icon-btn"
            :loading="startingIndex"
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

    <!-- 构建进度横幅（任务活跃 / 失败 / 有剩余段时显示，权威计数来自 getStatus） -->
    <div
      v-if="showBuildBanner"
      class="build-progress-panel"
      :class="{ 'build-progress-panel--error': buildModel.taskStatus === 'failed' }"
    >
      <div class="build-progress-head">
        <span class="build-progress-title">
          <Database :size="14" />
          {{
            buildModel.active
              ? buildStatusLabel
              : buildModel.taskStatus === 'failed'
                ? '上次索引未完成'
                : '索引未完成'
          }}
        </span>
        <span class="build-progress-summary">{{ buildStatusSummary(buildModel) }}</span>
      </div>
      <a-progress
        :percent="buildModel.active ? buildModel.taskProgress : buildModel.percent"
        :status="
          buildModel.taskStatus === 'failed' ? 'exception' : buildModel.active ? 'active' : 'normal'
        "
        size="small"
        :show-info="false"
      />
      <div v-if="buildModel.taskStatus === 'failed'" class="build-progress-detail">
        {{
          buildModel.taskMessage ||
          buildModel.taskError ||
          '存在待重试段落，可再次点击「开始索引」续跑'
        }}
      </div>
    </div>

    <!-- 设置浮动面板（纯显示；生产策略在「构建与发布」工作区） -->
    <transition name="slide-fade">
      <div v-if="showSettings" class="floating-panel settings-panel">
        <div class="panel-header">
          <span class="panel-title">图谱显示设置</span>
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
            <a-form-item label="画布会话过滤（仅本画布，不改生产检索）">
              <a-radio-group v-model:value="sessionFilter" size="small">
                <a-radio-button value="inherit">跟随生产策略</a-radio-button>
                <a-radio-button value="approved_only">只看已批准</a-radio-button>
                <a-radio-button value="candidates_visible">显示候选</a-radio-button>
              </a-radio-group>
              <div class="full-graph-hint">
                生产检索策略（review_policy）在「构建与发布」工作区变更并写审计；此处只影响画布显示。
              </div>
            </a-form-item>
            <a-form-item label="抽取器配置">
              <a-button v-if="isLlmGraphAllowed" size="small" @click="openGraphConfig">
                {{ graphConfigured ? '修改抽取配置' : '配置抽取器' }}
              </a-button>
              <div v-if="isLlmGraphAllowed" class="full-graph-hint">
                抽取器类型、模型与并发等构建配置；修改仅影响后续构建。
              </div>
              <div v-else class="full-graph-hint">
                规范图谱契约（managed_graph）禁止 LLM 自动抽取；图谱内容通过「托管导入」维护。
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

    <GraphExtractorConfigModal
      v-model:open="showGraphConfig"
      :kb-id="kbId"
      :status="graphBuildStatus"
      @saved="onGraphConfigSaved"
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
import {
  RefreshCw,
  Settings,
  Search,
  Loader2,
  FileUp,
  Download,
  Network,
  BrainCircuit,
  ScanText,
  ClipboardCheck,
  Database,
  RotateCcw
} from '@lucide/vue'
import GraphCanvas from '@/components/GraphCanvas.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'
import GraphImportModal from '@/components/GraphImportModal.vue'
import GraphReviewQueue from '@/components/GraphReviewQueue.vue'
import GateReviewQueue from '@/components/GateReviewQueue.vue'
import ConflictQueue from '@/components/ConflictQueue.vue'
import GraphExtractorConfigModal from '@/components/graph/GraphExtractorConfigModal.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import { unifiedApi } from '@/apis/graph_api'
import { graphExportApi } from '@/apis/knowledge_api'
import { message, Modal } from 'ant-design-vue'
import { useGraph } from '@/composables/useGraph'
import { normalizeBuildStatus, buildStatusSummary, buildTaskLabel } from '@/utils/graph/reviewMeta'

const DEFAULT_GRAPH_VIEW_SETTINGS = Object.freeze({
  maxNodes: 100,
  maxDepth: 2,
  excludeChunk: true,
  fullGraph: false
})

const props = defineProps({
  active: { type: Boolean, default: false }
})

const store = useDatabaseStore()
const taskerStore = useTaskerStore()

const kbId = computed(() => store.kbId)

// ---- 知识源契约感知：入口收敛 ----
const kbContractKey = computed(() => String(store.database?.contract_key || '').trim())
const isGraphImportAllowed = computed(() => {
  const key = kbContractKey.value
  return !key || key === 'legacy_generic' || key === 'legacy_mixed' || key === 'managed_graph'
})
const isLlmGraphAllowed = computed(() => kbContractKey.value !== 'managed_graph')

const graphRef = ref(null)
const showSettings = ref(false)
const subgraphParams = reactive({ ...DEFAULT_GRAPH_VIEW_SETTINGS })
const settingsForm = reactive({ ...DEFAULT_GRAPH_VIEW_SETTINGS })
// 画布会话级显示过滤（不持久化、不影响检索）：inherit = 跟随生产策略
const sessionFilter = ref('inherit')
const highlightKeywords = computed(() =>
  searchInput.value.trim() ? [searchInput.value.trim()] : []
)
const graphSettingsLoadedKbId = ref('')
const graphSettingsLoading = ref(false)
// 模块级加载状态：历史提交中声明遗失、只剩使用处（loadGraphSettings 读到未声明变量即
// ReferenceError，被上层 try/catch 吞掉导致视图设置持久化静默失效）——此处恢复声明
let graphSettingsLoadPromise = null
let graphSettingsLoadKbId = ''
let graphSettingsRequestSeq = 0
const searchInput = ref('')
// 对话关系图深链：?seed= 预填画布检索词（一次性，不覆盖用户后续输入）
{
  const seedFromRoute = String(
    (typeof window !== 'undefined' && window.location.search.match(/[?&]seed=([^&]+)/) || [])[1] || ''
  )
  if (seedFromRoute) {
    try {
      searchInput.value = decodeURIComponent(seedFromRoute)
    } catch {
      searchInput.value = seedFromRoute
    }
  }
}
const showGraphConfig = ref(false)
const showGraphImport = ref(false)
const showReviewQueue = ref(false)
const showGateReviewQueue = ref(false)
const graphBuildStatus = ref(null)
const graphBuildLoading = ref(false)
let buildStatusPollTimer = null
let graphStatusRequestSeq = 0
let graphLoadRequestSeq = 0

const isBuildActive = computed(() => {
  const s = graphBuildStatus.value?.build_task_status
  return s === 'pending' || s === 'running'
})

const graphSettingsSaving = ref(false)
// 抽取器是否已配置（决定空态引导与工具栏高亮）
const graphConfigured = computed(() => Boolean(graphBuildStatus.value?.configured))
const hasPendingGraphChunks = computed(
  () => Number(graphBuildStatus.value?.pending_chunks ?? 0) > 0
)
// 构建进度展示模型（归一化，杜绝「已开始索引  个段落」这类空数字）
const buildModel = computed(() => normalizeBuildStatus(graphBuildStatus.value))
const buildStatusLabel = computed(() => buildTaskLabel(buildModel.value))
const showBuildBanner = computed(
  () =>
    buildModel.value.active ||
    buildModel.value.taskStatus === 'failed' ||
    buildModel.value.pending > 0
)

const toggleSettingsPanel = async () => {
  const opening = !showSettings.value
  showSettings.value = opening
  if (opening) {
    await loadGraphSettings()
    Object.assign(settingsForm, subgraphParams)
  }
}

const openGraphImport = () => {
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

const graph = reactive(useGraph(graphRef))
const graphLoaded = ref(false)

const hasGraphNodes = computed(() => graph.graphData.nodes.length > 0)
const showGraphDataEmpty = computed(
  () => graphLoaded.value && !graph.fetching && !hasGraphNodes.value
)
const graphDataEmptyTitle = computed(() =>
  searchInput.value.trim() ? '未找到匹配实体' : '暂无知识图谱'
)
const graphDataEmptyDescription = computed(() => {
  if (searchInput.value.trim()) return '换个关键词或调整图谱显示设置后再搜索。'
  if (isBuildActive.value) return '图谱索引正在运行，完成后会展示实体与关系。'
  if (!isLlmGraphAllowed.value) {
    return '规范图谱库的内容通过「托管导入」维护：使用工具栏的导入按钮上传节点/关系 CSV，或先在发布治理中构建发布清单。'
  }
  return '当前知识库还没有可展示的实体与关系；可在「构建与发布」工作区配置抽取器并开始索引。'
})

let pendingLoadTimer = null

const getErrorDetail = (e, fallback) => {
  return e?.response?.data?.detail || e?.response?.data?.message || e?.message || fallback
}

const normalizeGraphViewSettings = (value = {}) => ({
  maxNodes: Math.min(1000, Math.max(10, Number(value.max_nodes) || 100)),
  maxDepth: Math.min(5, Math.max(1, Number(value.max_depth) || 2)),
  excludeChunk: typeof value.exclude_chunk === 'boolean' ? value.exclude_chunk : true
})

const graphTotalEntities = computed(() => {
  const value = Number(graphBuildStatus.value?.entity_count)
  return Number.isFinite(value) ? value : null
})

const graphTotalRelationships = computed(() => {
  const value = Number(graphBuildStatus.value?.relationship_count)
  return Number.isFinite(value) ? value : null
})

const graphTruncated = ref(false)

const resetGraphViewSettings = () => {
  Object.assign(subgraphParams, DEFAULT_GRAPH_VIEW_SETTINGS)
  Object.assign(settingsForm, DEFAULT_GRAPH_VIEW_SETTINGS)
  sessionFilter.value = 'inherit'
}

const loadGraphSettings = async (force = false) => {
  const currentDatabaseId = kbId.value
  if (!currentDatabaseId) return false
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
        message.warning('读取图谱显示设置失败，已暂时使用默认值')
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
  if (!kbId.value) return
  const requestSeq = ++graphStatusRequestSeq
  const currentDatabaseId = kbId.value
  graphBuildLoading.value = true
  try {
    const { graphBuildApi } = await import('@/apis/knowledge_api')
    const status = await graphBuildApi.getStatus(currentDatabaseId)
    if (requestSeq === graphStatusRequestSeq && currentDatabaseId === kbId.value) {
      graphBuildStatus.value = status
    }
  } catch (e) {
    console.error('Failed to load graph build status:', e)
  } finally {
    if (requestSeq === graphStatusRequestSeq) {
      graphBuildLoading.value = false
    }
  }
}

const openGraphConfig = () => {
  showGraphConfig.value = true
}

const onGraphConfigSaved = async () => {
  await loadGraphBuildStatus()
}

const startingIndex = ref(false)

const startGraphBuild = async () => {
  if (!kbId.value || startingIndex.value || isBuildActive.value) return
  startingIndex.value = true
  try {
    const { graphBuildApi } = await import('@/apis/knowledge_api')
    const data = await graphBuildApi.startIndex(kbId.value, 20)
    if (data?.status === 'already_running') {
      message.info('图谱索引正在运行中')
    } else {
      // 后端权威计数：queued_count = 提交时待构建段数；0 时为无任务提示而非成功
      const queued = Number(data?.queued_count ?? 0)
      if (data?.status === 'failed') {
        message.error(data?.message || '提交失败，请稍后重试')
      } else if (queued > 0) {
        message.success(`图谱索引任务已提交：本次待构建 ${queued.toLocaleString('zh-CN')} 段`)
      } else {
        message.info(data?.message || '没有待构建段落，无需索引')
      }
    }
    await loadGraphBuildStatus()
    startBuildStatusPoll()
  } catch (e) {
    console.error('Failed to start graph build:', e)
    message.error(getErrorDetail(e, '启动图谱索引失败'))
  } finally {
    startingIndex.value = false
  }
}

const confirmGraphReset = () => {
  Modal.confirm({
    title: '清空并重建图谱',
    content:
      '将清空当前图谱并重抽所有段落；人工审核决策与已固定证据会在重建后按决策恢复。确定继续？',
    okText: '清空并重建',
    okButtonProps: { danger: true },
    cancelText: '取消',
    async onOk() {
      try {
        const { graphBuildApi } = await import('@/apis/knowledge_api')
        await graphBuildApi.reset(kbId.value, { confirm: true })
        message.success('图谱已清空，开始重建')
        graph.clearGraph()
        await loadGraphBuildStatus()
        startBuildStatusPoll()
      } catch (e) {
        console.error('Failed to reset graph:', e)
        message.error(getErrorDetail(e, '重建图谱失败'))
      }
    }
  })
}

const loadGraph = async () => {
  if (!kbId.value) return

  const requestedDatabaseId = kbId.value
  await loadGraphSettings()
  if (requestedDatabaseId !== kbId.value) {
    // 等待期间库已切换：让位给当前库的调度，绝不静默中止（冷启动白板的根因之一）
    if (kbId.value) scheduleGraphLoad(0)
    return
  }

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
    // 会话级显示过滤：仅影响画布查询，绝不影响 Graph-RAG 检索
    if (sessionFilter.value !== 'inherit') {
      requestParams.review_policy = sessionFilter.value
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
    // 纯显示设置：不再携带 review_policy（生产策略走治理端点 + 审计）
    const response = await unifiedApi.updateViewSettings(currentDatabaseId, {
      max_nodes: settingsForm.maxNodes,
      max_depth: settingsForm.maxDepth,
      exclude_chunk: settingsForm.excludeChunk
    })
    if (currentDatabaseId !== kbId.value) return
    const settings = normalizeGraphViewSettings(response?.data)
    Object.assign(subgraphParams, settings, { fullGraph: settingsForm.fullGraph })
    Object.assign(settingsForm, settings, { fullGraph: settingsForm.fullGraph })
    graphSettingsLoadedKbId.value = currentDatabaseId
    showSettings.value = false
    message.success(response?.message || '图谱显示设置已保存')
    await loadGraph()
  } catch (e) {
    console.error('Failed to save graph view settings:', e)
    message.error(getErrorDetail(e, '保存图谱显示设置失败'))
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
      exclude_chunk: subgraphParams.excludeChunk
    })
  } catch (e) {
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
  if (!props.active || !kbId.value) {
    return
  }

  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
  }
  pendingLoadTimer = setTimeout(async () => {
    pendingLoadTimer = null
    await nextTick()
    if (props.active && kbId.value) {
      await loadGraph()
    }
  }, delay)
}

watch(sessionFilter, () => loadGraph())

// 联合就绪触发：active 与 kbId 各自异步就位（冷启动直达时顺序不定），
// 两者齐备才首载；谁后到谁补枪——取代原先两个会互相错过的独立触发器。
const graphBootstrappedFor = ref('')
watch(
  () => [props.active, kbId.value],
  ([active, kb]) => {
    if (!active || !kb) return
    if (graphBootstrappedFor.value === kb) return
    graphBootstrappedFor.value = kb
    loadGraphBuildStatus()
    scheduleGraphLoad()
  },
  { immediate: true }
)

watch(kbId, (_next, previous) => {
  graphStatusRequestSeq += 1
  graphLoadRequestSeq += 1
  graphSettingsRequestSeq += 1
  graphSettingsLoadedKbId.value = ''
  graphSettingsLoadPromise = null
  graphSettingsLoadKbId = ''
  graphSettingsLoading.value = false
  graphBootstrappedFor.value = ''
  resetGraphViewSettings()
  graphLoaded.value = false
  graph.clearGraph()
  graphBuildStatus.value = null
  if (previous) {
    loadGraphBuildStatus()
    scheduleGraphLoad(300)
  }
})

defineExpose({ openGraphConfig, loadGraph, loadGraphBuildStatus })

onUnmounted(() => {
  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
    pendingLoadTimer = null
  }
  stopBuildStatusPoll()
})
</script>

<style scoped lang="less">
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
  pointer-events: none;

  .actions-left,
  .actions-right {
    pointer-events: auto;
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

    &.attention-btn {
      color: var(--main-color);
      background: var(--main-10);
    }
  }

  .search-suffix-icon {
    cursor: pointer;
  }

  .spin {
    animation: spin 1s linear infinite;
  }
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }

  to {
    transform: rotate(360deg);
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
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.build-progress-panel {
  position: absolute;
  top: 56px;
  left: 50%;
  transform: translateX(-50%);
  width: min(520px, calc(100% - 32px));
  z-index: 90;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;

  .build-progress-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 6px;
  }

  .build-progress-title {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    color: var(--gray-900);
    white-space: nowrap;
  }

  .build-progress-summary {
    color: var(--gray-600);
    font-size: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .build-progress-detail {
    margin-top: 6px;
    color: var(--color-warning-500);
    font-size: 12px;
    line-height: 1.4;
  }

  &.build-progress-panel--error {
    border-color: var(--color-warning-500);
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
