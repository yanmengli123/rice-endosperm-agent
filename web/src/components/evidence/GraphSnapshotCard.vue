<template>
  <section ref="cardRoot" class="graph-snapshot-card">
    <header class="graph-snapshot-header">
      <div>
        <strong>关系图</strong>
        <span class="graph-snapshot-meta">
          {{ snapshot.nodes.length }} 个对象 · {{ snapshot.edges.length }} 组关系
          <template v-if="rawEdgeCount > snapshot.edges.length"
            >（聚合自 {{ rawEdgeCount }} 条抽取记录）</template
          >
        </span>
      </div>
      <div class="graph-snapshot-badges">
        <span v-if="snapshot.conflict_present" class="badge badge-conflict">存在冲突</span>
        <span v-if="pendingEdges" class="badge badge-pending">含 {{ pendingEdges }} 条待审核</span>
        <span v-if="snapshot.truncated" class="badge">已截取</span>
        <span class="badge">{{ pendingEdges ? '已审核 + 待审核候选' : '已审核关系' }}</span>
        <button
          v-if="runId"
          type="button"
          class="badge badge-download"
          :disabled="Boolean(exportingFormat)"
          title="下载 JSON（含投影哈希与完整载荷）"
          @click="downloadExport('json')"
        >
          {{ exportingFormat === 'json' ? '导出中…' : '↓ JSON' }}
        </button>
        <button
          v-if="runId"
          type="button"
          class="badge badge-download"
          :disabled="Boolean(exportingFormat)"
          title="下载 CSV 压缩包（nodes / edges / manifest）"
          @click="downloadExport('csv')"
        >
          {{ exportingFormat === 'csv' ? '导出中…' : '↓ CSV' }}
        </button>
      </div>
    </header>
    <p v-if="exportError" class="graph-snapshot-hint detail-error" role="alert">
      {{ exportError }}
    </p>
    <div v-if="canvasError" class="graph-snapshot-hint graph-snapshot-canvas-error">
      关系图画布暂不可用（{{ canvasError }}），完整关系见下方分组列表，或使用导出按钮获取数据。
    </div>
    <div
      v-else-if="renderFailed"
      class="graph-snapshot-hint graph-snapshot-canvas-error"
      role="alert"
    >
      关系图画布多次初始化未成功。
      <button type="button" class="badge badge-download" @click="retryCanvas">重新渲染</button>
      完整关系见下方分组列表，或使用导出按钮获取数据。
    </div>
    <div
      v-else-if="!inView"
      class="graph-snapshot-canvas graph-snapshot-canvas-idle"
      aria-hidden="true"
    >
      <span class="graph-snapshot-hint">关系图画布已随滚动回收，重新进入视野自动恢复。</span>
    </div>
    <div
      v-else
      class="graph-snapshot-canvas"
      role="img"
      :aria-label="`实体关系子图（可视化概览）：${snapshot.nodes.length} 个对象、${snapshot.edges.length} 组关系，画布仅绘制支持度前 ${canvasEdgeCount} 组；完整关系的文本版见下方关系列表`"
    >
      <GraphCanvas
        :key="canvasKey"
        :graph-data="canvasData"
        :truncated="Boolean(snapshot.truncated)"
        :limit-editable="false"
        :display-limit="snapshot.nodes.length"
        :highlight-keywords="seedNames"
        compact
        @node-click="openNode"
        @edge-click="openEdge"
        @canvas-click="selection = null"
        @render-failed="renderFailed = true"
      />
    </div>
    <div v-if="fullListTruncatedInCanvas" class="graph-snapshot-hint">
      画布仅绘制支持度前 {{ canvasEdgeCount }} 组关系，完整
      {{ snapshot.edges.length }} 组见下方列表。
    </div>
    <div
      v-if="relationGroups.length"
      class="graph-snapshot-groups"
      role="region"
      aria-label="关系列表（图形的文本替代，含全部关系）"
    >
      <section v-for="group in relationGroups" :key="group.group" class="relation-group">
        <div class="relation-group-title">
          {{ group.label }}<span class="relation-group-count">{{ group.edges.length }} 组</span>
        </div>
        <button
          v-for="edge in group.edges"
          :key="edge.triple_id"
          type="button"
          class="relation-item"
          @click="openEdgeFromList(edge)"
        >
          <span class="relation-target">{{ targetName(edge) }}</span>
          <span class="relation-predicates">{{
            (edge.predicates || [edge.predicate]).join(' / ')
          }}</span>
          <span class="relation-meta">
            <span v-if="Number(edge.parallel_count) > 1" class="meta-chip"
              >×{{ edge.parallel_count }}</span
            >
            <span v-if="knowledgeBaseIds.length > 1" class="meta-chip" :title="edge.kb_id">
              来源 {{ edge.kb_id }}
            </span>
            <span class="meta-chip">支持 {{ edge.support_count }}</span>
            <span
              v-if="String(edge.review_status).toUpperCase() === 'CANDIDATE'"
              class="meta-chip meta-pending"
              >待审核</span
            >
            <span
              v-else-if="Number(edge.candidate_parallel_count) > 0"
              class="meta-chip meta-pending"
              >含 {{ edge.candidate_parallel_count }} 条待审核</span
            >
            <span v-if="edge.conflict_status !== 'NONE'" class="meta-chip meta-conflict">冲突</span>
          </span>
        </button>
      </section>
    </div>
    <p v-if="snapshot.truncated" class="graph-snapshot-hint">
      关系较多，仅展示本轮范围内排序靠前的有界子图。
    </p>
    <div v-if="workbenchRoute" class="graph-snapshot-hint graph-workbench-link">
      <button type="button" class="badge badge-download" @click="openWorkbench">
        在图谱工作台打开完整图 →
      </button>
    </div>
    <p v-if="suppressedCandidates" class="graph-snapshot-hint">
      另有
      {{ suppressedCandidates }}
      条待审核候选关系未展示：可在图谱审核工作台完成审核，或由管理员为本库开启候选证据策略。
    </p>
    <div v-if="selection" class="graph-snapshot-detail">
      <div class="detail-title">{{ selectionTitle }}</div>
      <div v-if="detailLoading" class="detail-muted">正在读取规范层证据…</div>
      <div v-else-if="candidateNote" class="detail-muted">{{ candidateNote }}</div>
      <div v-else-if="detailError" class="detail-error">{{ detailError }}</div>
      <template v-else-if="detail">
        <div v-if="detail.trust_tier" class="detail-muted">可信等级：{{ detail.trust_tier }}</div>
        <div v-if="detail.review_status" class="detail-muted">
          审核状态：{{ detail.review_status }}
        </div>
        <div
          v-for="(mention, index) in detail.mentions || []"
          :key="`${mention.chunk_id}-${index}`"
          class="mention"
        >
          <div class="mention-source">
            {{ mention.filename || mention.file_id || '来源文档'
            }}<span v-if="mention.page"> · 第 {{ mention.page }} 页</span>
          </div>
          <blockquote>{{ mention.quote || '该记录没有可发布的逐字引文。' }}</blockquote>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, onErrorCaptured, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import GraphCanvas from '@/components/GraphCanvas.vue'
import { graphApi } from '@/apis/graph_api'
import { apiGet } from '@/apis'
import {
  exportGraphSnapshotUrl,
  graphCanvasData,
  graphWorkbenchRoute,
  groupEdgesByRelationGroup,
  pendingEdgeCount,
  suppressedCandidateCount
} from '@/utils/graphSnapshot'

const CANVAS_EDGE_LIMIT = 60
const props = defineProps({
  snapshot: { type: Object, required: true },
  runId: { type: String, default: '' }
})
const router = useRouter()
const selection = ref(null)
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const candidateNote = ref('')
const canvasData = computed(() => graphCanvasData(props.snapshot, CANVAS_EDGE_LIMIT))
const canvasEdgeCount = computed(() => canvasData.value.edges.length)
const fullListTruncatedInCanvas = computed(
  () => props.snapshot.edges.length > canvasEdgeCount.value
)
const relationGroups = computed(() => groupEdgesByRelationGroup(props.snapshot))
const rawEdgeCount = computed(() => Number(props.snapshot.total_raw_edge_count || 0))
const nameById = computed(() => {
  const map = new Map()
  for (const node of props.snapshot.nodes || []) map.set(String(node.entity_id), String(node.name))
  return map
})
const seedNames = computed(() =>
  props.snapshot.nodes.filter((node) => node.is_seed).map((node) => String(node.name))
)
const pendingEdges = computed(() => pendingEdgeCount(props.snapshot))
const suppressedCandidates = computed(() => suppressedCandidateCount(props.snapshot))
const knowledgeBaseIds = computed(() =>
  Array.from(
    new Set(
      [...(props.snapshot.nodes || []), ...(props.snapshot.edges || [])]
        .map((item) => String(item?.kb_id || '').trim())
        .filter(Boolean)
    )
  ).sort()
)

// 错误边界：可视化附件绝不允许拖死宿主聊天页——画布初始化失败时降级为纯列表，
// 拦截错误不再向上传播（完整关系仍可经分组列表与导出获得）。
const canvasError = ref('')
onErrorCaptured((error) => {
  canvasError.value = error?.message || String(error)
  return false
})

// ── 画布生命周期治理 ─────────────────────────────────────────────
// 懒挂载/离屏回收：IntersectionObserver 驱动，长会话中离屏卡不占用 G6 实例；
// 回收后重新进入视野自动重建（canvasKey 强制全新实例，避免复用异常）。
const cardRoot = ref(null)
const inView = ref(false)
const canvasKey = ref(0)
const renderFailed = ref(false)
let viewportObserver = null
onMounted(() => {
  if (!cardRoot.value || typeof IntersectionObserver === 'undefined') {
    inView.value = true
    return
  }
  viewportObserver = new IntersectionObserver(
    (entries) => {
      const visible = entries.some((entry) => entry.isIntersecting)
      if (visible && !inView.value) canvasKey.value += 1
      inView.value = visible
    },
    { rootMargin: '300px 0px', threshold: 0 }
  )
  viewportObserver.observe(cardRoot.value)
})
onBeforeUnmount(() => {
  if (viewportObserver) {
    viewportObserver.disconnect()
    viewportObserver = null
  }
})
const retryCanvas = () => {
  renderFailed.value = false
  canvasKey.value += 1
}

// ── 图谱工作台深链：携带主库与种子实体，落地页 tab=graph 直达 ──────
const workbenchRoute = computed(() => graphWorkbenchRoute(props.snapshot))
const openWorkbench = () => {
  if (workbenchRoute.value) router.push(workbenchRoute.value)
}

const exportingFormat = ref('')
const exportError = ref('')
const downloadExport = async (format) => {
  if (!props.runId || exportingFormat.value) return
  exportError.value = ''
  exportingFormat.value = format
  try {
    const response = await apiGet(exportGraphSnapshotUrl(props.runId, format), {}, true, 'blob')
    const blob = await response.blob()
    const disposition =
      response.headers.get('Content-Disposition') || response.headers.get('content-disposition')
    let filename = `graph-snapshot.${format === 'csv' ? 'zip' : 'json'}`
    if (disposition) {
      const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
      if (utf8Match?.[1]) {
        try {
          filename = decodeURIComponent(utf8Match[1])
        } catch {
          /* 沿用兜底文件名 */
        }
      } else {
        const asciiMatch = disposition.match(/filename="?([^";]+)"?/i)
        filename = asciiMatch?.[1] || filename
      }
    }
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch (error) {
    console.warn('graph snapshot export failed:', error)
    exportError.value = '关系图导出失败。请重试；若权限刚刚调整，请刷新会话后再试。'
  } finally {
    exportingFormat.value = ''
  }
}

const targetName = (edge) => {
  const seedIds = new Set(
    (props.snapshot.nodes || [])
      .filter((node) => node.is_seed)
      .map((node) => String(node.entity_id))
  )
  const other = seedIds.has(String(edge.source_entity_id))
    ? edge.target_entity_id
    : edge.source_entity_id
  return nameById.value.get(String(other)) || String(other)
}
const openEdgeFromList = (edge) => {
  selection.value = { ...edge, name: targetName(edge) }
  if (String(edge.review_status || '').toUpperCase() === 'CANDIDATE') {
    detail.value = null
    detailError.value = ''
    detailLoading.value = false
    candidateNote.value =
      '待审核候选关系：该边来自自动抽取、尚未通过人工审核，证据详情在审核通过后提供。'
    return
  }
  loadDetail('edge', edge)
}
const selectionTitle = computed(
  () => selection.value?.name || selection.value?.predicate || '关系证据'
)

const original = (event) => event?.data?.original || event?.original || null
const loadDetail = async (kind, item) => {
  selection.value = item
  detail.value = null
  detailError.value = ''
  candidateNote.value = ''
  detailLoading.value = true
  try {
    const response =
      kind === 'edge'
        ? await graphApi.getTripleEvidence(item.kb_id, item.triple_id)
        : await graphApi.getEntityEvidence(item.kb_id, item.entity_id)
    detail.value = response?.data || response || null
  } catch (error) {
    detailError.value = error?.message || '证据读取失败'
  } finally {
    detailLoading.value = false
  }
}
const openNode = (event) => {
  const item = original(event)?.properties
  if (item?.entity_id) loadDetail('node', item)
}
const openEdge = (event) => {
  const item = original(event)?.properties
  if (!item?.triple_id) return
  // 待审核候选边：属导航平面，不提供证据抽屉（无 claim 级合格证据可发布）
  if (String(item.review_status || '').toUpperCase() === 'CANDIDATE') {
    selection.value = item
    detail.value = null
    detailError.value = ''
    detailLoading.value = false
    candidateNote.value =
      '待审核候选关系：该边来自自动抽取、尚未通过人工审核，证据详情在审核通过后提供。'
    return
  }
  loadDetail('edge', item)
}
</script>

<style scoped>
.graph-snapshot-card {
  margin-top: 14px;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  overflow: hidden;
  background: var(--color-bg-container);
}
.graph-snapshot-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--gray-150);
}
.graph-snapshot-meta,
.detail-muted {
  margin-left: 8px;
  color: var(--gray-550);
  font-size: 12px;
}
.graph-snapshot-badges {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.badge {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--gray-100);
  color: var(--gray-650);
  font-size: 11px;
}
.badge-conflict {
  background: #fff1f0;
  color: #cf1322;
}
.badge-pending {
  background: #fffbe6;
  color: #ad6800;
}
.graph-snapshot-canvas-error {
  padding: 12px;
}
.badge-download {
  border: 0;
  cursor: pointer;
  background: var(--gray-100);
  color: var(--gray-650);
}
.badge-download:hover {
  background: var(--gray-200, #e5e6eb);
}
.badge-download:disabled {
  cursor: wait;
  opacity: 0.6;
}
.graph-snapshot-canvas {
  height: 360px;
}
.graph-snapshot-canvas-idle {
  display: flex;
  align-items: center;
  justify-content: center;
}
.graph-snapshot-hint {
  margin: 0;
  padding: 8px 12px;
  color: var(--gray-550);
  font-size: 12px;
  border-top: 1px solid var(--gray-150);
}
.graph-snapshot-detail {
  max-height: 260px;
  overflow: auto;
  padding: 10px 12px;
  border-top: 1px solid var(--gray-150);
}
.detail-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.detail-error {
  color: #cf1322;
}
.mention {
  padding: 8px 0;
  border-top: 1px solid var(--gray-100);
}
.mention-source {
  color: var(--gray-550);
  font-size: 12px;
}
blockquote {
  margin: 6px 0 0;
  padding-left: 10px;
  border-left: 3px solid var(--gray-200);
  white-space: pre-wrap;
}
.graph-snapshot-groups {
  max-height: 340px;
  overflow: auto;
  border-top: 1px solid var(--gray-150);
  padding: 6px 12px 10px;
}
.relation-group {
  margin-top: 8px;
}
.relation-group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--gray-650);
  margin-bottom: 4px;
}
.relation-group-count {
  margin-left: 8px;
  font-weight: 400;
  color: var(--gray-550);
}
.relation-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  border-bottom: 1px dashed var(--gray-100);
  padding: 6px 2px;
  cursor: pointer;
  font-size: 12px;
  color: inherit;
}
.relation-item:hover {
  background: var(--gray-100);
}
.relation-target {
  flex: 0 1 auto;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 46%;
}
.relation-predicates {
  flex: 0 1 auto;
  color: var(--gray-550);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.relation-meta {
  margin-left: auto;
  display: flex;
  gap: 4px;
  flex-wrap: nowrap;
}
.meta-chip {
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--gray-100);
  color: var(--gray-650);
  font-size: 11px;
  white-space: nowrap;
}
.meta-pending {
  background: #fffbe6;
  color: #ad6800;
}
.meta-conflict {
  background: #fff1f0;
  color: #cf1322;
}
</style>
