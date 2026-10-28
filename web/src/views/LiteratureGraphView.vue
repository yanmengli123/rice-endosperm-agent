<template>
  <div class="graph-view">
    <div class="graph-head">
      <h2>文献计量图谱</h2>
      <div class="graph-actions">
        <div class="mode-switch" role="group" aria-label="图谱显示模式">
          <button class="btn" :class="{ active: mode === '2d' }" @click="mode = '2d'">2D</button>
          <button class="btn" :class="{ active: mode === '3d' }" @click="mode = '3d'">3D</button>
        </div>
        <input v-model.number="limit" class="limit-input" type="number" min="10" max="2000" />
        <button class="btn" :disabled="loading" @click="load(false)">
          {{ loading ? '读取中…' : '计算图谱' }}
        </button>
        <button class="btn" @click="fitGraph">适配画布</button>
        <button class="btn" :disabled="projecting" @click="project">
          {{ projecting ? '投影中…' : '投影到 Neo4j' }}
        </button>
      </div>
    </div>
    <details class="graph-filters" open>
      <summary>图谱过滤（当前权限范围，先过滤再截断）</summary>
      <div class="graph-actions">
        <label
          >起始年<input v-model.number="filters.year_min" type="number" min="1000" max="9999"
        /></label>
        <label
          >截止年<input v-model.number="filters.year_max" type="number" min="1000" max="9999"
        /></label>
        <label
          >库内被引 ≥<input v-model.number="filters.min_library_citations" type="number" min="0"
        /></label>
        <label
          >外部被引 ≥<input v-model.number="filters.min_times_cited" type="number" min="0"
        /></label>
        <label
          >IF ≥<input v-model.number="filters.min_impact_factor" type="number" min="0" step="0.1"
        /></label>
        <label
          >聚类<input v-model.number="filters.cluster" type="number" min="0" placeholder="全部"
        /></label>
        <label
          >分区<select v-model="filters.quartiles" multiple aria-label="图谱分区">
            <option v-for="q in ['Q1', 'Q2', 'Q3', 'Q4', 'N/A']" :key="q" :value="q">
              {{ q }}
            </option>
          </select></label
        >
        <label
          >节点大小来源<select v-model="filters.citation_source">
            <option value="filtered">筛选后被引</option>
            <option value="library">当前可访问库内被引</option>
            <option value="wos">外部（导入 / 补全）被引</option>
          </select></label
        >
        <label
          >截断排序<select v-model="filters.sort_by">
            <option value="library_citations">库内被引</option>
            <option value="times_cited">外部被引</option>
            <option value="impact_factor">影响因子</option>
            <option value="paper_rank">PaperRank</option>
            <option value="year">年份</option>
          </select></label
        >
        <label><input v-model="filters.in_kb_only" type="checkbox" />仅当前成功 L1 知识库</label>
        <label
          ><input v-model="filters.exclude_references" type="checkbox" />排除参考文献节点</label
        >
        <label
          ><input v-model="filters.exclude_isolated" type="checkbox" />隐藏筛选后孤立节点</label
        >
        <button class="btn" :disabled="loading" @click="load(true)">预览节点数</button>
        <button class="btn" :disabled="loading" @click="resetFilters">重置过滤</button>
      </div>
      <p v-if="previewMeta" role="status">
        匹配 {{ previewMeta.matched_nodes }} 篇；按当前上限、孤立过滤后可显示
        {{ previewMeta.actual_nodes }} 篇。预览不替换现有画布。
      </p>
    </details>
    <div class="graph-actions" v-if="mode === '3d'" aria-label="3D 视角">
      <button
        v-for="(label, direction) in { front: '前', top: '顶', side: '侧', iso: '等轴' }"
        :key="direction"
        class="btn"
        @click="graph3d?.fit(direction)"
      >
        {{ label }}
      </button>
    </div>
    <details class="graph-filters">
      <summary>布局与样式</summary>
      <div class="graph-actions">
        <label
          >2D 布局<select v-model="layout" :disabled="mode === '3d'">
            <option value="fa2">ForceAtlas2（worker）</option>
            <option value="force">D3 力导向（非 ForceAtlas2）</option>
            <option value="circular">环形</option>
            <option value="grid">网格</option>
            <option value="random">稳定随机</option>
          </select></label
        >
        <label
          >布局间距<input v-model.number="spacing" type="number" min="0.25" max="5" step="0.25"
        /></label>
        <label
          >斥力倍率<input v-model.number="scaling" type="number" min="0.1" max="10" step="0.1"
        /></label>
        <label
          >引力倍率<input v-model.number="gravity" type="number" min="0.1" max="10" step="0.1"
        /></label>
        <label v-if="mode === '3d'"
          >3D 布局<select v-model="layout3d">
            <option value="force3d">D3 三维力导向</option>
            <option value="fa2">ForceAtlas2 平面投影</option>
          </select></label
        >
        <button class="btn" @click="applyLayout">重新计算布局</button>
        <button class="btn" :disabled="!layoutBusy" @click="pauseLayout">暂停布局</button>
        <label
          >着色<select v-model="graphStyle.colorMode">
            <option value="quartile">分区</option>
            <option value="if">影响因子</option>
            <option value="cluster">共引聚类</option>
            <option value="year">年份</option>
            <option value="citations">当前被引来源</option>
          </select></label
        >
        <label
          >渐变<select v-model="graphStyle.palette">
            <option value="viridis">Viridis</option>
            <option value="blues">蓝色</option>
            <option value="warm">暖色</option>
          </select></label
        >
        <label
          >大小缩放<select v-model="graphStyle.scaleAlgo">
            <option value="sqrt">平方根</option>
            <option value="pow06">幂 0.6</option>
            <option value="log">对数</option>
            <option value="linear">线性（有上限）</option>
            <option value="uniform">等大</option>
          </select></label
        >
        <label
          >节点倍率<input
            v-model.number="graphStyle.sizeBase"
            type="number"
            min="0.25"
            max="4"
            step="0.25"
        /></label>
        <label
          >标签<select v-model="graphStyle.labelMode">
            <option value="selected">仅选中</option>
            <option value="none">隐藏</option>
            <option value="title">标题</option>
            <option value="rid">标识</option>
            <option value="year_cited">年份 / 被引</option>
          </select></label
        >
        <label><input v-model="graphStyle.showEdges" type="checkbox" />显示引用边</label>
        <label><input v-model="graphStyle.showEdgeDir" type="checkbox" /> 引用方向</label>
        <label
          >边宽<input
            v-model.number="graphStyle.edgeWidth"
            type="number"
            min="0"
            max="5"
            step="0.25"
        /></label>
        <label>边色<input v-model="graphStyle.edgeColor" type="color" /></label>
        <label
          >背景<select v-model="graphStyle.background">
            <option value="theme">跟随主题</option>
            <option value="light">亮色</option>
            <option value="dark">暗色</option>
            <option value="custom">自定义</option>
          </select></label
        >
        <input
          v-if="graphStyle.background === 'custom'"
          v-model="graphStyle.bgColor"
          type="color"
          aria-label="自定义图谱背景色"
        />
        <button class="btn" @click="graphStyle = graphStyleDefaults()">重置样式</button>
      </div>
      <p v-if="layoutNotice" role="status">{{ layoutNotice }}</p>
    </details>
    <div class="graph-actions">
      <input
        v-model="locateQuery"
        placeholder="定位标题 / 文献标识"
        aria-label="图谱节点定位"
        @keyup.enter="locateNode"
      />
      <button class="btn" @click="locateNode">定位节点</button>
      <button class="btn" :disabled="!selected" @click="$emit('open-paper', selected)">
        阅读选中文献
      </button>
    </div>
    <div v-if="notice" class="graph-notice">{{ notice }}</div>
    <div class="graph-stats">
      <span>节点 {{ nodes.length }}</span>
      <span>边 {{ edges.length }}</span>
      <span v-if="network.truncated">（已按所选排序截断）</span>
      <span class="hint"
        >{{ mode === '3d' ? '左键旋转 · 右键平移 · 滚轮缩放' : '拖拽节点' }} · 点击查看邻居 ·
        节点大小={{ citationLabels[network.meta?.citation_source || 'library'] }}</span
      >
    </div>
    <div class="canvas-wrap">
      <canvas
        v-show="mode === '2d'"
        ref="canvas"
        width="900"
        height="560"
        @mousedown="onMouseDown"
        @mousemove="onMouseMove"
        @mouseup="onMouseUp"
        @mouseleave="onMouseUp"
        @wheel.prevent="onWheel"
      ></canvas>
      <LiteratureGraph3D
        v-if="visited3d"
        v-show="mode === '3d'"
        ref="graph3d"
        :nodes="nodes"
        :edges="edges"
        :selected-rid="selected?.rid || ''"
        :style-options="graphStyle"
        :positions="positions3d"
        :active="mode === '3d'"
        @select="selectNode"
      />
    </div>
    <div v-if="selected" class="neighbor-panel">
      <strong>{{ selected.title || selected.rid }}</strong>
      <div v-if="neighbors" class="neighbor-list">
        <div>参考文献 {{ neighbors.reference_count }}：</div>
        <div class="rid-list">
          <button
            v-for="rid in neighbors.references"
            :key="rid"
            class="btn"
            @click="locateNeighbor(rid)"
          >
            {{ rid }}</button
          ><span v-if="!neighbors.references.length">（无）</span>
        </div>
        <div>被引 {{ neighbors.cited_by_count }}：</div>
        <div class="rid-list">
          <button
            v-for="rid in neighbors.cited_by"
            :key="rid"
            class="btn"
            @click="locateNeighbor(rid)"
          >
            {{ rid }}</button
          ><span v-if="!neighbors.cited_by.length">（无）</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, shallowRef, onMounted, onUnmounted, onActivated, onDeactivated, watch } from 'vue'
import { createLiteratureLayoutRunner } from '@/utils/literature_layout_runner'
import { apiGet, apiPost } from '@/apis/base'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import { useThemeStore } from '@/stores/theme'
import LiteratureGraph3D from '@/components/literature/LiteratureGraph3D.vue'
import {
  canvasPoint,
  graphGeometry,
  graphStyleDefaults,
  graphNodeRadius,
  graphNodeColor,
  graphNodeLabel
} from '@/utils/literature_graph'
import { forceSimulation, forceManyBody, forceLink, forceCenter, forceCollide } from 'd3'

export default {
  name: 'LiteratureGraphView',
  components: { LiteratureGraph3D },
  emits: ['open-paper'],
  setup() {
    const themeStore = useThemeStore()
    const canvas = ref(null)
    const nodes = ref([])
    const edges = ref([])
    const network = ref({})
    const neighbors = ref(null)
    const selected = ref(null)
    const limit = ref(200)
    const loading = ref(false)
    const previewMeta = ref(null)
    const defaultFilters = () => ({
      year_min: '',
      year_max: '',
      min_library_citations: 0,
      min_times_cited: 0,
      min_impact_factor: 0,
      quartiles: [],
      cluster: '',
      in_kb_only: false,
      exclude_references: false,
      exclude_isolated: true,
      citation_source: 'filtered',
      sort_by: 'library_citations'
    })
    const filters = ref(defaultFilters())
    const citationLabels = { filtered: '筛选后被引', library: '可访问库内被引', wos: '外部被引' }
    const notice = ref('')
    const projecting = ref(false)
    const mode = ref('2d')
    const graph3d = ref(null)
    const locateQuery = ref('')
    const layout = ref('fa2')
    const layout3d = ref('force3d')
    const visited3d = ref(false)
    const positions3d = shallowRef(null)
    const scaling = ref(1),
      gravity = ref(1)
    const layoutBusy = ref(false),
      layoutNotice = ref('')
    const spacing = ref(1)
    const graphStyle = ref(graphStyleDefaults())
    const viewport = ref({ x: 0, y: 0, scale: 1 })
    let pointerStart = null
    let panning = false
    let moved = false
    let generation = 0
    let neighborGeneration = 0
    let disposed = false
    let raf = null
    let dragNode = null
    let simNodes = []
    let simEdges = []
    let simulation
    let userMoved = false
    const layoutRunner = createLiteratureLayoutRunner(
      (data) => {
        layoutBusy.value = !data.done
        layoutNotice.value = data.done
          ? `布局已计算 ${data.completed} 步${data.reason === 'budget' ? '（达到时间预算，可继续重算）' : ''}`
          : `布局计算中：${data.completed} 步`
        if (mode.value === '3d') positions3d.value = data.positions
        else {
          simNodes.forEach((node, index) => {
            node.x = data.positions[index * 3] + 450
            node.y = data.positions[index * 3 + 1] + 280
          })
          if (!userMoved) fitGraph()
        }
      },
      (error) => {
        layoutBusy.value = false
        layoutNotice.value = error
      }
    )
    const pauseLayout = () => {
      layoutRunner.stop()
      simulation?.alpha(0)
      layoutBusy.value = false
    }

    const load = async (preview = false) => {
      const ticket = ++generation
      loading.value = true
      notice.value = ''
      try {
        const data = await literatureWorkspaceApi.graphNetwork({
          ...filters.value,
          limit: limit.value || 200,
          preview
        })
        if (disposed || ticket !== generation) return
        if (preview) {
          previewMeta.value = data.meta
          return
        }
        previewMeta.value = null
        network.value = data || {}
        nodes.value = data?.nodes || []
        edges.value = data?.edges || []
        positions3d.value = null
        pauseLayout()
        initSimulation()
        applyLayout()
        selected.value = null
        neighbors.value = null
        neighborGeneration++
        viewport.value = { x: 0, y: 0, scale: 1 }
        if (data.meta?.isolated_removed)
          notice.value = `已隐藏 ${data.meta.isolated_removed} 个筛选后孤立节点；可取消该过滤查看。`
      } catch (error) {
        if (!disposed && ticket === generation) notice.value = error.message || '图谱加载失败'
      } finally {
        if (!disposed && ticket === generation) loading.value = false
      }
    }
    const resetFilters = () => {
      filters.value = defaultFilters()
      load(false)
    }

    const project = async () => {
      projecting.value = true
      notice.value = ''
      try {
        const result = await apiPost('/api/literature/graph/project')
        notice.value = `投影完成：${result.nodes} 节点 / ${result.edges} 边（PG 仍为权威源）`
      } catch (err) {
        notice.value = `投影失败：${err?.message || err}`
      } finally {
        projecting.value = false
      }
    }

    const initSimulation = () => {
      const width = 900
      const height = 560
      simNodes = nodes.value.map((n, i) => {
        const angle = (2 * Math.PI * i) / Math.max(1, nodes.value.length)
        const radius = 180 + (i % 7) * 22
        return {
          ...n,
          x: width / 2 + radius * Math.cos(angle),
          y: height / 2 + radius * Math.sin(angle),
          vx: 0,
          vy: 0
        }
      })
      const byRid = Object.fromEntries(simNodes.map((n) => [n.rid, n]))
      simEdges = edges.value
        .map((e) => ({ source: byRid[e.source], target: byRid[e.target] }))
        .filter((e) => e.source && e.target)
      simulation?.stop()
      simulation = forceSimulation(simNodes)
        .stop()
        .force('charge', forceManyBody().strength(-80))
        .force('link', forceLink(simEdges).distance(90).strength(0.15))
        .force('center', forceCenter(width / 2, height / 2))
        .force(
          'collision',
          forceCollide((node) => 9 + Math.min(14, Math.sqrt(node.citations || 0) * 2.2))
        )
    }

    const step = () => {
      // Barnes-Hut charge/collision trees replace the unbounded O(N²) frame loop.
      if (layout.value === 'force' && layoutBusy.value) {
        if (simulation?.alpha() > 0.005) simulation.tick()
        else layoutBusy.value = false
      }
    }

    const draw = () => {
      const ctx = canvas.value?.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, 900, 560)
      if (graphStyle.value.background !== 'theme') {
        ctx.fillStyle =
          graphStyle.value.background === 'dark'
            ? '#111827'
            : graphStyle.value.background === 'light'
              ? '#ffffff'
              : graphStyle.value.bgColor
        ctx.fillRect(0, 0, 900, 560)
      }
      ctx.save()
      ctx.translate(viewport.value.x, viewport.value.y)
      ctx.scale(viewport.value.scale, viewport.value.scale)
      ctx.strokeStyle =
        graphStyle.value.edgeColor ||
        (themeStore.isDark ? 'rgba(105, 177, 255, 0.3)' : 'rgba(22, 119, 255, 0.25)')
      const edgeWidth = Math.max(0, Math.min(5, Number(graphStyle.value.edgeWidth) || 0))
      ctx.lineWidth = Math.max(0.01, edgeWidth)
      for (const edge of graphStyle.value.showEdges && edgeWidth > 0 ? simEdges : []) {
        ctx.beginPath()
        ctx.moveTo(edge.source.x, edge.source.y)
        ctx.lineTo(edge.target.x, edge.target.y)
        ctx.stroke()
        if (graphStyle.value.showEdgeDir) {
          const angle = Math.atan2(edge.target.y - edge.source.y, edge.target.x - edge.source.x)
          const radius = graphNodeRadius(edge.target, graphStyle.value) + 2
          const x = edge.target.x - Math.cos(angle) * radius,
            y = edge.target.y - Math.sin(angle) * radius
          ctx.beginPath()
          ctx.moveTo(x - Math.cos(angle - 0.5) * 8, y - Math.sin(angle - 0.5) * 8)
          ctx.lineTo(x, y)
          ctx.lineTo(x - Math.cos(angle + 0.5) * 8, y - Math.sin(angle + 0.5) * 8)
          ctx.stroke()
        }
      }
      for (const node of simNodes) {
        const radius = graphNodeRadius(node, graphStyle.value)
        ctx.beginPath()
        ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI)
        ctx.fillStyle = graphNodeColor(node, graphStyle.value, themeStore.isDark)
        ctx.fill()
        if (node.rid === selected.value?.rid) {
          ctx.strokeStyle = '#f5222d'
          ctx.lineWidth = 2
          ctx.stroke()
        }
        const label = graphNodeLabel(node, graphStyle.value.labelMode, selected.value?.rid)
        if (label) {
          ctx.fillStyle =
            graphStyle.value.background === 'dark' ||
            (graphStyle.value.background === 'theme' && themeStore.isDark)
              ? '#eeeeee'
              : '#222222'
          ctx.font = '12px sans-serif'
          ctx.fillText(label, node.x + radius + 3, node.y + 4)
        }
      }
      ctx.restore()
    }

    const tick = () => {
      if (disposed || mode.value !== '2d') return
      step()
      draw()
      raf = requestAnimationFrame(tick)
    }

    const nodeAt = (x, y) =>
      simNodes.find(
        (n) => Math.hypot(n.x - x, n.y - y) < graphNodeRadius(n, graphStyle.value) + 4
      ) || null

    const localCoords = (event) => {
      const rect = canvas.value.getBoundingClientRect()
      return canvasPoint(event.clientX, event.clientY, rect, viewport.value)
    }

    const onMouseDown = (event) => {
      const point = localCoords(event)
      if (!point) return
      pointerStart = {
        x: event.clientX,
        y: event.clientY,
        panX: viewport.value.x,
        panY: viewport.value.y
      }
      moved = false
      panning = event.button === 1 || (event.button === 0 && event.shiftKey)
      if (panning) event.preventDefault()
      dragNode = !panning && event.button === 0 ? nodeAt(point.x, point.y) : null
      if (dragNode) {
        pauseLayout()
        userMoved = true
        dragNode.fx = dragNode.x
        dragNode.fy = dragNode.y
        simulation?.alpha(0.3)
      }
    }
    const onMouseMove = (event) => {
      if (!pointerStart) return
      const dx = event.clientX - pointerStart.x,
        dy = event.clientY - pointerStart.y
      if (Math.hypot(dx, dy) > 3) moved = true
      if (panning) {
        if (moved) userMoved = true
        const rect = canvas.value.getBoundingClientRect()
        viewport.value.x = pointerStart.panX + (dx * 900) / rect.width
        viewport.value.y = pointerStart.panY + (dy * 560) / rect.height
      } else if (dragNode) {
        const point = localCoords(event)
        if (point) {
          dragNode.x = dragNode.fx = point.x
          dragNode.y = dragNode.fy = point.y
          simulation?.alpha(0.3)
        }
      }
    }
    const onMouseUp = async (event) => {
      const clicked = pointerStart && !moved && !panning && event.type === 'mouseup'
      if (dragNode) {
        dragNode.fx = null
        dragNode.fy = null
      }
      dragNode = null
      pointerStart = null
      panning = false
      const point = clicked ? localCoords(event) : null
      if (point) await selectNode(nodeAt(point.x, point.y))
    }

    const onWheel = (event) => {
      userMoved = true
      const point = localCoords(event)
      if (!point) return
      const old = viewport.value.scale
      const scale = Math.max(0.2, Math.min(5, old * (event.deltaY < 0 ? 1.15 : 1 / 1.15)))
      viewport.value = {
        x: viewport.value.x + point.x * (old - scale),
        y: viewport.value.y + point.y * (old - scale),
        scale
      }
    }
    const fitGraph = () => {
      viewport.value = { x: 0, y: 0, scale: 1 }
      if (simNodes.length) {
        let minX = Infinity,
          minY = Infinity,
          maxX = -Infinity,
          maxY = -Infinity
        for (const node of simNodes) {
          minX = Math.min(minX, node.x - 24)
          maxX = Math.max(maxX, node.x + 24)
          minY = Math.min(minY, node.y - 24)
          maxY = Math.max(maxY, node.y + 24)
        }
        const scale = Math.max(0.05, Math.min(3, 900 / (maxX - minX), 560 / (maxY - minY)))
        viewport.value = {
          x: 450 - ((minX + maxX) / 2) * scale,
          y: 280 - ((minY + maxY) / 2) * scale,
          scale
        }
      }
      if (mode.value === '3d') graph3d.value?.fit()
    }
    const locateNode = () => {
      const query = locateQuery.value.trim().toLocaleLowerCase()
      if (!query) return
      const exact = nodes.value.find(
        (node) => node.rid.toLocaleLowerCase() === query || node.doi?.toLocaleLowerCase() === query
      )
      const matches = exact
        ? [exact]
        : nodes.value.filter((node) => (node.title || '').toLocaleLowerCase().includes(query))
      if (matches.length !== 1) {
        notice.value = matches.length
          ? '匹配多个节点，请输入精确文献标识。'
          : '当前图谱未找到该节点（可调整节点上限）。'
        return
      }
      selectNode(matches[0])
      const node = simNodes.find((item) => item.rid === matches[0].rid)
      if (mode.value === '2d' && node)
        viewport.value = { x: 450 - node.x, y: 280 - node.y, scale: 1 }
      if (mode.value === '3d') graph3d.value?.locate(matches[0].rid)
    }
    const locateNeighbor = (rid) => {
      locateQuery.value = rid
      locateNode()
    }

    const selectNode = async (node) => {
      const ticket = ++neighborGeneration
      selected.value = node
      neighbors.value = null
      if (!node) return
      try {
        const data = await apiGet(`/api/literature/graph/neighbors/${encodeURIComponent(node.rid)}`)
        if (!disposed && ticket === neighborGeneration) neighbors.value = data
      } catch (error) {
        if (!disposed && ticket === neighborGeneration)
          notice.value = error.message || '邻居读取失败'
      }
    }

    const applyLayout = () => {
      pauseLayout()
      simulation?.stop()
      userMoved = false
      if (!nodes.value.length) return
      const algorithm = mode.value === '3d' ? layout3d.value : layout.value
      if (['fa2', 'force3d'].includes(algorithm)) {
        layoutBusy.value = true
        const input = nodes.value.map((node, index) => ({
          ...node,
          ...(mode.value === '3d' && positions3d.value
            ? {
                layoutX: positions3d.value[index * 3],
                layoutY: positions3d.value[index * 3 + 1],
                layoutZ: positions3d.value[index * 3 + 2]
              }
            : {})
        }))
        layoutRunner.start(
          input,
          edges.value.map((edge) => ({ ...edge })),
          {
            algorithm,
            spacing: spacing.value,
            scaling: scaling.value,
            gravity: gravity.value,
            style: { ...graphStyle.value },
            iterations: 300
          }
        )
        return
      }
      if (algorithm === 'force') {
        initSimulation()
        simulation?.alpha(1)
        layoutBusy.value = true
      } else {
        simulation?.stop()
        const positions = new Map(
          graphGeometry(nodes.value, layout.value, spacing.value).map((node) => [node.rid, node])
        )
        for (const node of simNodes)
          Object.assign(node, positions.get(node.rid), { vx: 0, vy: 0, fx: null, fy: null })
      }
      fitGraph()
    }
    watch([layout, layout3d, spacing, scaling, gravity], () => {
      positions3d.value = null
      applyLayout()
    })

    onMounted(async () => {
      await load()
      if (!raf && mode.value === '2d') tick()
    })
    onUnmounted(() => {
      disposed = true
      generation++
      neighborGeneration++
      pauseLayout()
      simulation?.stop()
      if (raf) cancelAnimationFrame(raf)
    })
    watch(mode, () => {
      if (raf) cancelAnimationFrame(raf)
      pauseLayout()
      if (mode.value === '2d') tick()
      else {
        visited3d.value = true
        if (!positions3d.value) applyLayout()
      }
    })
    onDeactivated(() => {
      pauseLayout()
      if (raf) cancelAnimationFrame(raf)
      raf = null
    })
    onActivated(() => {
      if (!disposed && mode.value === '2d' && !raf) tick()
    })

    return {
      canvas,
      nodes,
      edges,
      network,
      neighbors,
      selected,
      limit,
      notice,
      projecting,
      mode,
      filters,
      loading,
      previewMeta,
      citationLabels,
      resetFilters,
      graph3d,
      locateQuery,
      layout,
      layout3d,
      visited3d,
      positions3d,
      scaling,
      gravity,
      layoutBusy,
      layoutNotice,
      pauseLayout,
      spacing,
      graphStyle,
      graphStyleDefaults,
      applyLayout,
      fitGraph,
      locateNode,
      locateNeighbor,
      onWheel,
      load,
      project,
      onMouseDown,
      onMouseMove,
      onMouseUp,
      selectNode
    }
  }
}
</script>

<style lang="less" scoped>
.graph-view {
  padding: 16px 24px;
}
.graph-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.graph-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.mode-switch {
  display: inline-flex;
  .btn {
    border-radius: 0;
  }
  .btn:first-child {
    border-radius: 6px 0 0 6px;
  }
  .btn:last-child {
    border-radius: 0 6px 6px 0;
  }
  .btn.active {
    color: var(--gray-0);
    background: var(--main-color);
    border-color: var(--main-color);
  }
}
.limit-input {
  width: 80px;
  padding: 4px 8px;
}
.graph-filters {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px;
  margin: 12px 0;
}
.graph-filters label {
  display: flex;
  align-items: center;
  gap: 6px;
}
.graph-filters input[type='number'] {
  width: 90px;
}
.graph-filters input,
.graph-filters select {
  background: var(--color-bg-container);
  color: var(--color-text-primary);
  border: 1px solid var(--gray-150);
  border-radius: 4px;
  padding: 4px;
}
.graph-notice {
  margin: 10px 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--main-color) 10%, transparent);
  font-size: 13px;
}
.graph-stats {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin: 8px 0;
  .hint {
    color: var(--color-text-secondary);
  }
}
.canvas-wrap {
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  canvas {
    display: block;
    max-width: 100%;
    cursor: grab;
  }
}
.neighbor-panel {
  margin-top: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
}
.neighbor-list {
  margin-top: 6px;
}
.rid-list {
  font-family: monospace;
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin: 2px 0 8px;
  word-break: break-all;
}
</style>
