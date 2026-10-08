<template>
  <transition name="slide-fade">
    <div v-if="open" class="floating-panel insight-panel">
      <div class="panel-header">
        <span class="panel-title">
          图谱洞察
          <span class="panel-subtitle">实体类型关联</span>
        </span>
        <button class="panel-close" type="button" title="关闭" @click="close">
          <X :size="14" />
        </button>
      </div>
      <div class="panel-body">
        <div class="scope-row">
          <a-radio-group :value="dataScope" size="small" @change="onDataScopeChange">
            <a-radio-button value="canvas">画布子图</a-radio-button>
            <a-radio-button value="full">全图</a-radio-button>
          </a-radio-group>
          <button
            v-if="dataScope === 'full'"
            class="panel-close"
            type="button"
            title="刷新全图数据"
            :disabled="fullLoading"
            @click="ensureFullData(true)"
          >
            <RotateCw :size="12" />
          </button>
        </div>
        <!-- 视图切换：三视图共享同一聚合模型与口径数字，仅呈现层不同 -->
        <div class="scope-row scope-row--views">
          <a-radio-group :value="activeView" size="small" @change="onViewChange">
            <a-radio-button value="chord">弦图</a-radio-button>
            <a-radio-button value="sankey">流向</a-radio-button>
            <a-radio-button value="heatmap">矩阵</a-radio-button>
            <a-radio-button value="vosviewer">VOSviewer</a-radio-button>
          </a-radio-group>
        </div>
        <div
          v-if="activeView === 'sankey' && sankeyCapped"
          class="insight-caliber insight-caliber--perf"
        >
          桑基视图按可读性上限绘制流量前 {{ sankeyData.meta.shownTypes }} 个类型（共
          {{ sankeyData.meta.typeCount }}），折叠
          {{ sankeyData.meta.omittedTypes.length }} 个类型、{{ sankeyData.meta.omittedWeight }}
          条跨类型关系及
          {{ sankeyData.meta.omittedInternalEdges }}
          条类型内部关系未绘制；完整类型分布可在矩阵中缩放查看。源在左、目标在右，同一类型可出现在两侧。
        </div>
        <div class="insight-caliber">
          统计范围：{{ scopeLabel }} · {{ policyLabel }} · 按去重关系计数
          <template v-if="loadedAt"> · {{ loadedAt }}</template>
        </div>
        <div v-if="!isFullScope && loading" class="insight-notice">
          {{ loadedAt ? '正在刷新，以下为上次成功加载的数据。' : '正在加载图谱数据。' }}
        </div>
        <div v-if="!isFullScope && loadError" class="insight-notice insight-notice--warn">
          {{ loadError }}
        </div>
        <div v-if="isFullScope && fullLoading" class="insight-notice">
          {{ fullData ? '正在刷新全图，以下为上次成功加载的数据。' : '正在加载全图数据…' }}
        </div>
        <div v-if="isFullScope && fullError" class="insight-notice insight-notice--warn">
          {{ fullError }}
          <button class="omitted-toggle" type="button" @click="ensureFullData(true)">重试</button>
        </div>
        <div v-if="projectionChanged" class="insight-notice insight-notice--warn">
          图谱正在更新，本次结果不完整，请刷新获取完整结果。
        </div>
        <div v-if="effectiveTruncated && !projectionChanged" class="insight-notice insight-notice--warn">
          查询已截断（{{ truncationLabel }}），<template v-if="isFullScope"
            >统计基于安全上限内的全图数据（可见 {{ visibleEntityCount }} / 全库
            {{ totalEntities ?? '—' }} 实体），不代表全库分布</template
          ><template v-else
            >以下统计基于当前展示子集（可见 {{ visibleEntityCount }} / 全库
            {{ totalEntities ?? '—' }} 实体），不代表全量分布</template
          >
        </div>
        <template v-if="sourceData">
          <GraphVOSviewerPanel
            v-if="activeView === 'vosviewer'"
            :kb-id="kbId"
            :data="sourceData"
            :pending="isFullScope ? fullLoading : loading"
            :truncated="effectiveTruncated"
            @refresh="isFullScope ? ensureFullData() : emit('refresh')"
            @access-denied="emit('access-denied')"
          />
          <template v-else>
          <div class="insight-summary">
            <span>{{ fullMeta.entityNodes }} 个实体 · {{ fullMeta.typeCount }} 个类型</span>
            <span>{{ fullMeta.relationEdges }} 条去重实体关系</span>
          </div>
          <!-- 口径对账：三段数字必须自洽（总 = 跨类型 + 类型内部 + 端点未解析） -->
          <div class="insight-caliber">
            其中 {{ fullMeta.crossTypeEdges }} 条跨类型<template v-if="fullMeta.selfLoopEdges > 0">
              · {{ fullMeta.selfLoopEdges }} 条类型内部</template
            ><template v-if="fullMeta.danglingEdges > 0">
              · {{ fullMeta.danglingEdges }} 条端点未解析（跳过）</template
            >
            · {{ fullMeta.isolatedTypes }} 个类型无关系 · 共 {{ fullMeta.relationTypeCount }} 种谓词
          </div>
          <div v-if="showScopeControl" class="insight-scope">
            <a-radio-group :value="scopeValue" size="small" @change="onScopeChange">
              <a-radio-button value="top">Top {{ activeTopN }}</a-radio-button>
              <a-radio-button value="all">全部已加载类型 {{ fullMeta.typeCount }}</a-radio-button>
            </a-radio-group>
            <div
              v-if="scopeValue === 'all' && fullMeta.typeCount > 120"
              class="insight-caliber insight-caliber--perf"
            >
              全量
              {{ fullMeta.typeCount }}
              类型属大规模渲染：已关闭动画并降低布局迭代，首帧仍可能需要数秒；矩阵视图开销最小。
            </div>
            <div v-if="viewData.meta.omittedTypes.length" class="insight-omitted">
              <div>
                已按关联量选取前 {{ viewData.meta.shownTypes }} 个类型，折叠
                {{ viewData.meta.omittedTypes.length }} 个类型、{{ viewData.meta.omittedWeight }}
                条跨类型关系及
                {{ viewData.meta.omittedInternalEdges }} 条类型内部关系未绘制（跨类型绘制
                {{ viewData.meta.drawnEdges }}/{{ fullMeta.crossTypeEdges }}
                条）
              </div>
              <div v-if="omittedPreview" class="omitted-preview">
                折叠中关联最多：{{ omittedPreview }}
              </div>
              <button
                class="omitted-toggle"
                type="button"
                @click="omittedExpanded = !omittedExpanded"
              >
                {{
                  omittedExpanded
                    ? '收起折叠清单'
                    : `展开折叠清单（${viewData.meta.omittedTypes.length} 个类型）`
                }}
              </button>
              <div v-if="omittedExpanded" class="omitted-list">
                <div
                  v-for="item in viewData.meta.omittedTypes"
                  :key="item.name"
                  class="omitted-row"
                  :title="`${item.name}: ${item.total} 条跨类型关联，${item.internalRelations} 条内部关系`"
                >
                  <span
                    class="type-color"
                    :style="{ backgroundColor: swatchColor(item.name) }"
                  ></span>
                  <span class="type-name">{{ item.name }}</span>
                  <span class="type-count">{{ item.total }}</span>
                </div>
              </div>
            </div>
          </div>
          <div class="insight-caliber">
            当前绘制：{{ viewData.meta.drawnEdges }} / {{ fullMeta.crossTypeEdges }} 条跨类型，
            {{ activeView === 'chord' ? 0 : viewData.meta.drawnInternalEdges }} /
            {{ fullMeta.selfLoopEdges }} 条类型内部关系。
            <template v-if="activeView === 'chord'">内部关系完整列于下表。</template>
            <template v-else-if="activeView === 'sankey'"
              >宽度表示关系数，方向不等于因果或物质流动。</template
            >
            <template v-else
              >行是源、列是目标；空白仅表示当前结果中无关系。超过 24
              类型可缩放和拖动查看。</template
            >
          </div>
          <div
            v-if="viewRenderable"
            ref="chartRef"
            class="insight-chart"
            role="img"
            :aria-label="viewAriaLabel"
          ></div>
          <div v-else class="insight-empty">{{ viewEmptyText }}</div>
          <details v-if="crossAssociations.length" class="pair-card">
            <summary>
              跨类型关联（{{ crossAssociations.length }} 组已绘制{{
                activeView === 'chord' ? '类型对' : '方向'
              }}）
            </summary>
            <div class="association-list">
              <button
                v-for="pair in crossAssociations"
                :key="JSON.stringify([pair.source, pair.target])"
                type="button"
                class="internal-row"
                @click="selectedPair = pair"
              >
                <span>{{ pair.source }} {{ pair.directed ? '→' : '↔' }} {{ pair.target }}</span>
                <span>{{ pair.value }} 条 · 查看</span>
              </button>
            </div>
          </details>
          <details
            v-if="chordData.internalRelations.length"
            class="pair-card"
            :open="!chordData.hasRibbons"
          >
            <summary>类型内部关系（{{ chordData.meta.selfLoopEdges }} 条，全部已加载类型）</summary>
            <button
              v-for="pair in chordData.internalRelations"
              :key="pair.source"
              type="button"
              class="internal-row"
              @click="selectedPair = pair"
            >
              <span>{{ pair.source }} → {{ pair.target }}</span
              ><span>{{ pair.value }} 条 · 查看</span>
            </button>
          </details>
          <!-- 弦带下钻：点弦带查看该类型对的方向构成与关系类型构成（开放词表语义还原） -->
          <div v-if="selectedPair" class="pair-card">
            <div class="pair-head">
              <span class="pair-title"
                >{{ selectedPair.source }}
                {{
                  selectedPair.directed || selectedPair.source === selectedPair.target ? '→' : '↔'
                }}
                {{ selectedPair.target }}</span
              >
              <span class="pair-total">{{ selectedPair.value }} 条</span>
              <button class="panel-close" type="button" title="关闭" @click="selectedPair = null">
                <X :size="12" />
              </button>
            </div>
            <div class="pair-section">
              <div class="pair-section-title">
                方向与谓词（{{ selectedPair.semantics.length }} 组，展开查看每条关系）
              </div>
              <details
                v-for="(row, index) in selectedPair.semantics"
                :key="index"
                class="semantic-row"
              >
                <summary>
                  {{ row.source }} → {{ row.target }} · {{ row.type }} · {{ row.count }} 条
                </summary>
                <div class="relation-list">
                  <button
                    v-for="edge in row.edges"
                    :key="edge.triple_id || edge.properties?.triple_id || edge.id"
                    type="button"
                    class="relation-row"
                    @click="emit('edge-select', edge)"
                  >
                    <span>{{ entityName(edge.source_id) }} → {{ entityName(edge.target_id) }}</span>
                    <span class="relation-action">{{ reviewLabel(edge) }} · 详情 / 原文证据</span>
                  </button>
                </div>
              </details>
            </div>
          </div>
          <details v-if="fullAgg.nodes.length" class="pair-card">
            <summary>实体类型清单（{{ fullMeta.typeCount }} 个，含孤立类型）</summary>
            <div class="association-list">
              <button
                v-for="node in fullAgg.nodes"
                :key="node.name"
                type="button"
                class="internal-row"
                @click="emit('type-select', node.name)"
              >
                <span>{{ node.name }}</span
                ><span>{{ node.entityCount }} 个实体 · {{ node.relationTotal }} 条关系</span>
              </button>
            </div>
          </details>
          <div class="insight-hint">
            三视图聚合的是实体类型，实体与关系详情通过下方清单查看。点击图中类型高亮实体；点击关联或矩阵单元格查看原始方向、谓词和证据。配色不表示可信程度。<template
              v-if="isFullScope"
              >当前为全图口径：类型高亮仅作用于画布已加载子图，画布中不存在的类型无法高亮。</template
            >
          </div>
          </template>
        </template>
      </div>
    </div>
  </transition>
</template>

<script setup>
import * as echarts from 'echarts'
import { RotateCw, X } from '@lucide/vue'
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { useUserStore } from '@/stores/user'
import { isGraphAccessDenied } from '@/utils/graph/graphAccess'
import { graphApi } from '@/apis/graph_api'
import GraphVOSviewerPanel from './GraphVOSviewerPanel.vue'
import {
  NODE_LABEL_COLORS,
  getPaletteColor,
  isEntityNode,
  lightenForDarkTheme
} from '@/utils/graph/graphPalette'
import {
  applyTypeLimit,
  buildChordData,
  resolveDefaultMaxTypes
} from '@/utils/graph/chartAggregations'
import { buildChordOption, buildHeatmapOption, buildSankeyOption } from '@/utils/graph/chartOptions'
import {
  formatChordTooltip,
  formatSankeyTooltip,
  makeHeatmapTooltip
} from '@/utils/graph/chartTooltips'

// CSS 变量解析工具函数（与 dashboard 图表组件同模式，主题切换时重渲染取新值）
function getCSSVariable(variableName, element = document.documentElement) {
  return getComputedStyle(element).getPropertyValue(variableName).trim()
}

const props = defineProps({
  open: { type: Boolean, default: false },
  // 画布已加载的图谱数据（原始 API 形状）：统计口径与用户当前视图一致
  graphData: { type: Object, required: true, default: () => ({ nodes: [], edges: [] }) },
  truncated: { type: Boolean, default: false },
  totalEntities: { type: Number, default: null },
  loading: { type: Boolean, default: false },
  loadError: { type: String, default: '' },
  // 全图口径独立拉取所需上下文（画布子图口径不使用）
  kbId: { type: String, default: '' },
  // 画布会话过滤口径（inherit 时为 null），全图拉取与其保持一致以便对账
  reviewPolicy: { type: String, default: null }
})

const emit = defineEmits(['update:open', 'type-select', 'edge-select', 'access-denied', 'refresh'])

const themeStore = useThemeStore()
const userStore = useUserStore()
const chartRef = ref(null)
const scopeValue = ref('top')
// 用户显式切换过档位后尊重其选择；否则默认档位随数据规模自适应
const scopeTouched = ref(false)
const omittedExpanded = ref(false)
const selectedPair = ref(null)
// 统计口径：canvas = 画布已加载子图（与画布强一致）；full = 全图独立拉取（安全上限内），
// 供「看全库分布但不重渲染大画布」的场景，切换必须显式并全程标注口径
const dataScope = ref('canvas')
const fullData = ref(null)
const fullLoading = ref(false)
const fullError = ref('')
let fullSeq = 0
let chart = null
let resizeObserver = null
let retryTimer = null
let retryCount = 0
const MAX_RETRIES = 10

const isFullScope = computed(() => dataScope.value === 'full')
const effectivePolicy = computed(
  () => props.reviewPolicy || props.graphData.meta?.review_policy || null
)
// 全图口径用独立拉取的数据（可能尚未就绪，为 null 时模板显示加载/错误态）；
// 画布口径沿用画布已加载子图（含口径 meta）
const sourceData = computed(() => (isFullScope.value ? fullData.value : props.graphData))
const scopeLabel = computed(() => (isFullScope.value ? '全图（安全上限内）' : '当前已加载子图'))
const effectiveTruncated = computed(() =>
  isFullScope.value ? Boolean(fullData.value?.truncated) : props.truncated
)
const projectionChanged = computed(() =>
  (sourceData.value?.meta?.truncation_reasons || []).includes('concurrent_update')
)

async function ensureFullData() {
  if (!props.kbId) return
  const kbId = props.kbId
  const reviewPolicy = effectivePolicy.value
  const seq = ++fullSeq
  fullLoading.value = true
  fullError.value = ''
  fullData.value = null
  selectedPair.value = null
  try {
    const params = {
      kb_id: kbId,
      node_label: '*',
      max_depth: 2,
      max_nodes: 1000,
      // 弦图聚合会过滤 Chunk/MENTIONS，预排除以免浪费节点安全上限
      exclude_chunk: true,
      full_graph: true
    }
    if (reviewPolicy) params.review_policy = reviewPolicy
    const res = await graphApi.getSubgraph(params)
    if (seq !== fullSeq || kbId !== props.kbId || reviewPolicy !== effectivePolicy.value) return
    if (!res?.success || !Array.isArray(res.data?.nodes) || !Array.isArray(res.data?.edges)) {
      throw new Error('Invalid graph query response')
    }
    const data = {
      nodes: res?.data?.nodes || [],
      edges: res?.data?.edges || [],
      truncated: Boolean(res?.data?.truncated),
      meta: {
        ...res.data.meta,
        truncation_reasons: res?.data?.truncation_reasons || []
      }
    }
    fullData.value = data
  } catch (e) {
    if (seq !== fullSeq) return
    if (isGraphAccessDenied(e)) {
      fullData.value = null
      selectedPair.value = null
      fullError.value = '当前知识库不可访问，图谱数据已清除。'
      emit('access-denied')
      return
    }
    console.error('Failed to load full graph for insight panel:', e)
    fullError.value = fullData.value
      ? '全图数据加载失败，以下为上次成功加载的数据；可点击重试。'
      : '全图数据加载失败，可点击重试。'
  } finally {
    if (seq === fullSeq) fullLoading.value = false
  }
}

function onDataScopeChange(e) {
  const scope = e.target.value
  if (scope === dataScope.value) return
  dataScope.value = scope
  // 数据口径切换后旧下钻/折叠选择失效：复位并由默认档位随新数据规模重新推导
  selectedPair.value = null
  omittedExpanded.value = false
  scopeTouched.value = false
  if (scope === 'full') ensureFullData()
}

// 知识库或会话过滤口径变化：全图缓存立即失效；处于全图口径时退回画布子图，绝不跨库展示
watch(
  [() => props.kbId, effectivePolicy, () => props.graphData.meta?.generated_at, () => userStore.token],
  (current, previous) => {
    fullSeq += 1
    fullLoading.value = false
    fullData.value = null
    fullError.value = ''
    selectedPair.value = null
    if (current[0] !== previous[0] || current[1] !== previous[1] || current[3] !== previous[3]) {
      dataScope.value = 'canvas'
    } else if (props.open && isFullScope.value) ensureFullData()
  }
)

const policyLabel = computed(
  () =>
    ({ approved_only: '已批准 / 规范关系', candidates_visible: '含 AI 候选关系' })[
      sourceData.value?.meta?.review_policy
    ] || '审核策略待确认'
)
const loadedAt = computed(() => {
  const time = sourceData.value?.meta?.generated_at
  return time ? new Date(time).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' }) : ''
})
const truncationLabel = computed(() => {
  const labels = {
    node_limit: '节点上限',
    edge_limit: '关系上限',
    seed_limit: '搜索种子上限',
    path_limit: '路径预算',
    concurrent_update: '图谱正在更新，请刷新获取完整结果',
    node_safety_cap: '节点安全上限',
    edge_safety_cap: '关系安全上限',
    full_graph_safety_cap: '全图安全上限'
  }
  const reasons = (sourceData.value?.meta?.truncation_reasons || []).map(
    (reason) => labels[reason] || reason
  )
  if (reasons.length) return reasons.join('、')
  // 兼容未携带截断原因的旧响应。
  return isFullScope.value ? '全图安全上限' : '展示上限'
})
const entityNames = computed(
  () => new Map((sourceData.value?.nodes || []).map((node) => [String(node.id), node.name]))
)
const entityName = (id) => entityNames.value.get(String(id)) || String(id)
const reviewLabel = (edge) =>
  ({ APPROVED: '已批准', CANONICAL: '规范关系', CANDIDATE: 'AI 候选' })[
    edge.properties?.review_status
  ] || (edge.properties?.managed_projection ? '规范关系' : '状态未知')

// 单遍聚合 + 廉价折叠：档位切换不重扫边（applyTypeLimit 与内建折叠等价，spec 锁定）
const fullAgg = computed(() => buildChordData(sourceData.value, { maxTypes: 0 }))

// 全量口径 meta（maxTypes=0）：typeCount 与自适应档位依据，不随 Top-N 折叠变化
const fullMeta = computed(() => fullAgg.value.meta)

const activeTopN = computed(() => resolveDefaultMaxTypes(fullMeta.value.typeCount))

const showScopeControl = computed(() => fullMeta.value.typeCount > 12)

const chordData = computed(() =>
  applyTypeLimit(
    fullAgg.value,
    scopeValue.value === 'all' || activeTopN.value === 0 ? 0 : activeTopN.value
  )
)

// 性能闸门：全量档超过 120 类型属大规模渲染（三视图关动画 + 桑基降布局迭代）
const LARGE_TYPE_COUNT = 120
const largeScale = computed(
  () => fullMeta.value.typeCount > LARGE_TYPE_COUNT || chordData.value.directedLinks.length > 300
)

// 桑基可读性上限：数百类型在 340px 画布内行高不足 1px，物理不可读。
// 桑基视图按流量取前 SANKEY_MAX_TYPES 个类型绘制（与档位取小），折叠损益
// 如实提示；完整全量分布由矩阵视图承载。
const SANKEY_MAX_TYPES = 50
const sankeyLimit = computed(() => {
  const effective =
    activeTopN.value === 0 ? SANKEY_MAX_TYPES : Math.min(activeTopN.value, SANKEY_MAX_TYPES)
  return scopeValue.value === 'all' ? SANKEY_MAX_TYPES : effective
})
const sankeyData = computed(() => applyTypeLimit(fullAgg.value, sankeyLimit.value))
const sankeyCapped = computed(
  () => sankeyData.value.meta.typeCount > sankeyData.value.meta.shownTypes
)
const viewData = computed(() =>
  activeView.value === 'sankey' ? sankeyData.value : chordData.value
)
const crossAssociations = computed(() =>
  activeView.value === 'chord'
    ? viewData.value.links
    : viewData.value.directedLinks.filter((link) => link.source !== link.target)
)

const visibleEntityCount = computed(
  () => (sourceData.value?.nodes || []).filter(isEntityNode).length
)

// ---------------------------------------------------------------------------
// 视图模型：三视图（弦图/流向桑基/矩阵热力）共享同一 chordData，仅呈现层不同
// ---------------------------------------------------------------------------
const activeView = ref('chord')
const onViewChange = (e) => {
  activeView.value = e.target.value
  selectedPair.value = null
}

const keptNodeNames = computed(() => chordData.value.nodes.map((n) => n.name))

const viewRenderable = computed(() => {
  if (activeView.value === 'vosviewer') return false
  if (activeView.value === 'heatmap') {
    // 保留孤立类型坐标，内部关系在对角线中展示。
    return chordData.value.nodes.length > 0
  }
  if (activeView.value === 'sankey') {
    return (sankeyData.value.directedLinks?.length || 0) > 0
  }
  return chordData.value.links.length > 0
})

const viewEmptyText = computed(() => {
  if (activeView.value === 'heatmap') {
    return '当前没有可绘制的类型关联（含类型内部关系）。可调整图谱显示设置或切换口径。'
  }
  if (activeView.value === 'sankey') {
    return '当前绘制范围没有有向关系。可调整类型范围；孤立类型见类型清单。'
  }
  return '当前没有可绘制的跨类型关联。类型内部关系见下表，并可在流向和矩阵中查看；孤立类型见类型清单。'
})

const viewAriaLabel = computed(() => {
  const m = chordData.value.meta
  if (activeView.value === 'sankey') {
    return `类型流向图，源在左、目标在右：${sankeyData.value.directedLinks.length} 组有向类型流，绘制 ${sankeyData.value.meta.drawnRelationEdges} 条去重关系（含类型内部）；可通过下方关联列表查看详情。`
  }
  if (activeView.value === 'heatmap') {
    return `类型矩阵：${chordData.value.nodes.length} × ${chordData.value.nodes.length} 邻接矩阵（行=源、列=目标，对角线为类型内部关系）；可通过下方关联列表查看详情。`
  }
  return `实体类型弦图：${chordData.value.links.length} 组类型关联，${m.drawnEdges} 条关系；可通过下方关联列表查看详情。`
})

// 有向视图直接使用点击方向的事实，不混入反向关系。
function onHeatmapCellClick(params) {
  if (params.data?.semantics && params.value?.[2] > 0) {
    selectedPair.value = { ...params.data, value: params.value[2] }
  }
}

const omittedPreview = computed(() =>
  (viewData.value.meta.omittedTypes || [])
    .slice(0, 3)
    .map((item) => `${item.name} ${item.total}`)
    .join(' · ')
)

const swatchColor = (name) => {
  const color = getPaletteColor(name, NODE_LABEL_COLORS)
  return themeStore.isDark ? lightenForDarkTheme(color) : color
}

const close = () => emit('update:open', false)

const onScopeChange = (e) => {
  scopeTouched.value = true
  scopeValue.value = e.target.value
  omittedExpanded.value = false
  selectedPair.value = null
}

// 默认档位随数据规模初始化/跟随（用户未显式选择前）
watch(
  () => fullMeta.value.typeCount,
  () => {
    if (scopeTouched.value || fullMeta.value.typeCount === 0) return
    scopeValue.value = activeTopN.value === 0 ? 'all' : 'top'
  },
  { immediate: true }
)

// ---------------------------------------------------------------------------
// echarts 生命周期：init → setOption → dispose，容器 0 尺寸时定时重试
// （照抄 CallStatsComponent 先例；数据规模 ≤ 类型数，每次全量重建成本可忽略）
// ---------------------------------------------------------------------------
function scheduleRetry() {
  if (retryTimer) clearTimeout(retryTimer)
  if (retryCount >= MAX_RETRIES) return
  retryCount += 1
  retryTimer = setTimeout(renderChart, 200)
}

function renderChart() {
  const container = chartRef.value
  if (!container || !props.open || !viewRenderable.value) {
    disposeChart()
    return
  }
  const { clientWidth, clientHeight } = container
  if (!clientWidth || !clientHeight) {
    scheduleRetry()
    return
  }
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
  retryCount = 0

  // 每次渲染重建实例（而非复用实例 setOption）：
  // 1) dispose 是即时销毁，不经差量拆除动画——大规模图（数百类型）切换的
  //    阻塞实测有 3.6s+ 来自旧元素的 leave 动画帧；
  // 2) 复用实例在部分状态下存在 ~180ms/帧的常驻重绘循环（面板空闲仍烧主线程，
  //    关面板即归零、canvas 尺寸恒定已排除 resize 回环）——重建实例根治。
  // 小图 init 成本可忽略（<5ms）。
  disposeChart()
  chart = echarts.init(container)
  if (window.ResizeObserver) {
    resizeObserver = new ResizeObserver(() => chart?.resize())
    resizeObserver.observe(container)
  }
  // 弧段/节点点击 → 画布高亮该类型实体（ExplorerWorkbench 接线到 highlightType）；
  // 弦带保留双向事实，桑基连线只下钻点击方向；
  // 热力图格子点击 → 下钻（对角线 → 类型内部关系）
  chart.on('click', (params) => {
    if (activeView.value === 'heatmap') return onHeatmapCellClick(params)
    if (params.dataType === 'node') {
      emit('type-select', params.data.typeName ?? params.data.name)
    } else if (params.dataType === 'edge') {
      const pair =
        activeView.value === 'sankey'
          ? viewData.value.directedLinks.find(
              (link) =>
                link.source === params.data.sourceType && link.target === params.data.targetType
            )
          : params.data
      if (pair) selectedPair.value = pair
    }
  })
  chart.setOption(buildOption())
}

function disposeChart() {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
  retryCount = 0
  if (chart) {
    chart.dispose()
    chart = null
  }
}

function buildOption() {
  const isDark = themeStore.isDark
  // 暗色主题适配（近黑面板底 --color-trans-light）：身份色保色相提亮度、
  // 连线透明度按主题、弧段分隔缝暗色用 gray-200——具体约束见 chartOptions 注释
  const tooltipBase = {
    confine: true,
    // 下钻依赖点击命中：tooltip 默认 enterable=true 会拦截其覆盖区域的 canvas 点击
    enterable: false,
    backgroundColor: getCSSVariable('--gray-0'),
    borderColor: getCSSVariable('--gray-200'),
    borderWidth: 1,
    textStyle: { color: getCSSVariable('--gray-600'), fontSize: 12 }
  }
  const theme = {
    isDark,
    lighten: lightenForDarkTheme,
    largeScale: largeScale.value,
    labelColor: getCSSVariable('--gray-800'),
    arcBorderColor: getCSSVariable(isDark ? '--gray-200' : '--color-bg-container'),
    ribbonOpacity: isDark ? 0.85 : 0.6,
    sankeyLinkOpacity: isDark ? 0.55 : 0.4,
    heatRamp: [
      getCSSVariable('--main-50') || '#eaf4f7',
      getCSSVariable('--main-300') || '#7fc0d4',
      getCSSVariable('--main-700') || '#046a82'
    ],
    tooltip: tooltipBase
  }
  const data = chordData.value
  if (activeView.value === 'sankey') {
    return buildSankeyOption(sankeyData.value, {
      ...theme,
      tooltip: { ...tooltipBase, formatter: formatSankeyTooltip }
    })
  }
  if (activeView.value === 'heatmap') {
    return buildHeatmapOption(data, {
      ...theme,
      tooltip: { ...tooltipBase, formatter: makeHeatmapTooltip(keptNodeNames.value) }
    })
  }
  return buildChordOption(data, {
    ...theme,
    tooltip: { ...tooltipBase, formatter: formatChordTooltip }
  })
}

watch(
  sourceData,
  () => {
    // 数据口径变化后旧弦带对象已失效，下钻卡片同步清理
    selectedPair.value = null
  },
  { deep: true, flush: 'post' }
)

watch(
  [() => props.open, chordData, () => themeStore.isDark, activeView],
  () => nextTick(renderChart),
  {
    flush: 'post',
    immediate: true
  }
)

// 关闭期间画布快照变化会使全图缓存失效，重新打开时补拉数据。
watch(
  () => props.open,
  (open) => {
    fullSeq += 1
    fullLoading.value = false
    fullData.value = null
    selectedPair.value = null
    if (open && isFullScope.value) ensureFullData()
  }
)

onUnmounted(() => {
  fullSeq += 1
  disposeChart()
})
</script>

<style scoped lang="less">
.floating-panel {
  position: absolute;
  top: 60px;
  right: 10px;
  width: min(420px, calc(100% - 20px));
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
  user-select: auto;

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

    .panel-subtitle {
      margin-left: 8px;
      font-weight: 400;
      font-size: 12px;
      color: var(--gray-500);
    }
  }

  .panel-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--gray-500);
    cursor: pointer;

    &:hover {
      background: var(--gray-100);
      color: var(--gray-800);
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.scope-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.insight-notice {
  margin-bottom: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;

  &--warn {
    background: var(--color-warning-50, rgba(250, 173, 20, 0.1));
    color: var(--color-warning-600, #d97706);
  }
}

.insight-empty {
  padding: 24px 8px;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
}

.insight-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: var(--gray-600);
  font-size: 12px;

  span::before {
    content: '';
    display: inline-block;
    width: 6px;
    height: 6px;
    margin-right: 5px;
    border-radius: 50%;
    background: var(--main-color);
    vertical-align: 1px;
  }
}

// 口径对账行：三段数字自洽，杜绝「总数含自环但弦图没画」的误导
.insight-caliber {
  margin: 4px 0 8px;
  color: var(--gray-500);
  font-size: 11px;
  line-height: 1.6;

  &--perf {
    margin: 6px 0 0;
    color: var(--color-warning-600, #d97706);
  }
}

.insight-scope {
  margin-bottom: 8px;

  .insight-omitted {
    margin-top: 6px;
    color: var(--gray-500);
    font-size: 11px;
    line-height: 1.6;
  }
}

.omitted-preview {
  color: var(--gray-600);
}

.omitted-toggle {
  margin-top: 4px;
  border: none;
  background: transparent;
  padding: 2px 0;
  color: var(--main-color);
  font-size: 11px;
  cursor: pointer;

  &:hover {
    text-decoration: underline;
  }
}

.omitted-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 10px;
  max-height: 160px;
  overflow-y: auto;
  margin-top: 6px;
  padding: 6px 8px;
  border: 1px solid var(--gray-100);
  border-radius: 6px;
}

.omitted-row {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  align-items: center;
  gap: 6px;
  min-height: 24px;
  font-size: 11px;
}

.type-color {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  box-shadow:
    0 0 0 1px var(--color-bg-container),
    0 0 0 2px var(--gray-200);
}

.type-name {
  min-width: 0;
  overflow: hidden;
  color: var(--gray-800);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.type-count {
  color: var(--gray-1000);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.insight-chart {
  width: 100%;
  height: 340px;
}

.internal-row,
.relation-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 6px 0;
  border: 0;
  border-bottom: 1px solid var(--gray-100);
  background: transparent;
  color: var(--gray-800);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  &:hover {
    color: var(--color-primary);
  }
  &:focus-visible {
    outline: 2px solid var(--color-primary);
  }
}

.semantic-row {
  margin-top: 6px;
  summary {
    cursor: pointer;
    color: var(--gray-800);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
}

.relation-list,
.association-list {
  max-height: 220px;
  overflow-y: auto;
}
.internal-row {
  overflow-wrap: anywhere;
}
.relation-row {
  flex-direction: column;
  overflow-wrap: anywhere;
}
.relation-action {
  color: var(--color-primary);
  font-size: 11px;
}

// 弦带下钻卡片：类型对的方向构成 + 关系类型构成
.pair-card {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--color-trans-light);

  .pair-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .pair-title {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    color: var(--gray-1000);
    font-weight: 600;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pair-total {
    color: var(--gray-600);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }

  .pair-section {
    &:not(:last-child) {
      margin-bottom: 6px;
      padding-bottom: 6px;
      border-bottom: 1px dashed var(--gray-100);
    }
  }

  .pair-section-title {
    margin-bottom: 2px;
    color: var(--gray-500);
    font-size: 11px;
  }

  .pair-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 20px;
    font-size: 11px;

    &--dim {
      color: var(--gray-600);
    }
  }

  .pair-more {
    color: var(--gray-500);
    font-size: 11px;
  }
}

.insight-hint {
  margin-top: 6px;
  color: var(--color-text-quaternary);
  font-size: 11px;
  line-height: 1.5;
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
