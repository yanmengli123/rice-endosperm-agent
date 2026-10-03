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
        <button class="btn" @click="load">加载</button>
        <button class="btn" :disabled="projecting" @click="project">
          {{ projecting ? '投影中…' : '投影到 Neo4j' }}
        </button>
      </div>
    </div>
    <div v-if="notice" class="graph-notice">{{ notice }}</div>
    <div class="graph-stats">
      <span>节点 {{ nodes.length }}</span>
      <span>边 {{ edges.length }}</span>
      <span v-if="network.truncated">（已按被引数截断）</span>
      <span class="hint">{{ mode === '3d' ? '左键旋转 · 右键平移 · 滚轮缩放' : '拖拽节点' }} · 点击查看邻居 · 节点大小=被引 · 颜色=分区</span>
    </div>
    <div class="canvas-wrap">
      <canvas v-show="mode === '2d'" ref="canvas" width="900" height="560" @mousedown="onMouseDown" @mousemove="onMouseMove" @mouseup="onMouseUp" @mouseleave="onMouseUp"></canvas>
      <LiteratureGraph3D
        v-if="mode === '3d'"
        :nodes="nodes"
        :edges="edges"
        :selected-rid="selected?.rid || ''"
        @select="selectNode"
      />
    </div>
    <div v-if="selected" class="neighbor-panel">
      <strong>{{ selected.title || selected.rid }}</strong>
      <div v-if="neighbors" class="neighbor-list">
        <div>参考文献 {{ neighbors.reference_count }}：</div>
        <div class="rid-list">{{ neighbors.references.join(' · ') || '（无）' }}</div>
        <div>被引 {{ neighbors.cited_by_count }}：</div>
        <div class="rid-list">{{ neighbors.cited_by.join(' · ') || '（无）' }}</div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { useThemeStore } from '@/stores/theme'
import LiteratureGraph3D from '@/components/literature/LiteratureGraph3D.vue'

// 分区语义色：亮色模式标准色，暗黑模式提亮（WCAG 对比度）
const QUARTILE_COLORS_LIGHT = { Q1: '#1677ff', Q2: '#52c41a', Q3: '#faad14', Q4: '#8c8c8c' }
const QUARTILE_COLORS_DARK = { Q1: '#69b1ff', Q2: '#95de64', Q3: '#ffd666', Q4: '#a6a6a6' }

export default {
  name: 'LiteratureGraphView',
  components: { LiteratureGraph3D },
  setup() {
    const themeStore = useThemeStore()
    const canvas = ref(null)
    const nodes = ref([])
    const edges = ref([])
    const network = ref({})
    const neighbors = ref(null)
    const selected = ref(null)
    const limit = ref(200)
    const notice = ref('')
    const projecting = ref(false)
    const mode = ref('2d')
    let raf = null
    let dragNode = null
    let simNodes = []
    let simEdges = []

    const load = async () => {
      const data = await apiGet(`/api/literature/graph/network?limit=${encodeURIComponent(String(limit.value || 200))}`)
      network.value = data || {}
      nodes.value = data?.nodes || []
      edges.value = data?.edges || []
      initSimulation()
      selected.value = null
      neighbors.value = null
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
        return { ...n, x: width / 2 + radius * Math.cos(angle), y: height / 2 + radius * Math.sin(angle), vx: 0, vy: 0 }
      })
      const byRid = Object.fromEntries(simNodes.map((n) => [n.rid, n]))
      simEdges = edges.value.map((e) => ({ source: byRid[e.source], target: byRid[e.target] })).filter((e) => e.source && e.target)
    }

    const step = () => {
      const width = 900
      const height = 560
      const repulsion = 3000
      const spring = 0.02
      const springLength = 90
      const damping = 0.85
      for (let i = 0; i < simNodes.length; i++) {
        for (let j = i + 1; j < simNodes.length; j++) {
          const a = simNodes[i]
          const b = simNodes[j]
          let dx = b.x - a.x
          let dy = b.y - a.y
          let dist = Math.max(20, Math.hypot(dx, dy))
          const force = repulsion / (dist * dist)
          dx /= dist
          dy /= dist
          a.vx -= force * dx
          a.vy -= force * dy
          b.vx += force * dx
          b.vy += force * dy
        }
      }
      for (const edge of simEdges) {
        const dx = edge.target.x - edge.source.x
        const dy = edge.target.y - edge.source.y
        const dist = Math.max(1, Math.hypot(dx, dy))
        const force = (dist - springLength) * spring
        const nx = (dx / dist) * force
        const ny = (dy / dist) * force
        edge.source.vx += nx
        edge.source.vy += ny
        edge.target.vx -= nx
        edge.target.vy -= ny
      }
      for (const node of simNodes) {
        if (node === dragNode) {
          node.vx = 0
          node.vy = 0
          continue
        }
        node.vx += (width / 2 - node.x) * 0.002
        node.vy += (height / 2 - node.y) * 0.002
        node.vx *= damping
        node.vy *= damping
        node.x = Math.max(30, Math.min(width - 30, node.x + node.vx))
        node.y = Math.max(30, Math.min(height - 30, node.y + node.vy))
      }
    }

    const draw = () => {
      const ctx = canvas.value?.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, 900, 560)
      ctx.strokeStyle = themeStore.isDark ? 'rgba(105, 177, 255, 0.3)' : 'rgba(22, 119, 255, 0.25)'
      ctx.lineWidth = 1
      for (const edge of simEdges) {
        ctx.beginPath()
        ctx.moveTo(edge.source.x, edge.source.y)
        ctx.lineTo(edge.target.x, edge.target.y)
        ctx.stroke()
      }
      for (const node of simNodes) {
        const radius = 4 + Math.min(14, Math.sqrt(node.citations || 0) * 2.2)
        ctx.beginPath()
        ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI)
        const palette = themeStore.isDark ? QUARTILE_COLORS_DARK : QUARTILE_COLORS_LIGHT
        ctx.fillStyle = palette[node.quartile] || (themeStore.isDark ? '#5a5a5a' : '#d9d9d9')
        ctx.fill()
        if (node === selected.value) {
          ctx.strokeStyle = '#f5222d'
          ctx.lineWidth = 2
          ctx.stroke()
        }
      }
    }

    const tick = () => {
      step()
      draw()
      raf = requestAnimationFrame(tick)
    }

    const nodeAt = (x, y) =>
      simNodes.find((n) => Math.hypot(n.x - x, n.y - y) < 16) || null

    const localCoords = (event) => {
      const rect = canvas.value.getBoundingClientRect()
      return { x: event.clientX - rect.left, y: event.clientY - rect.top }
    }

    const onMouseDown = (event) => {
      const { x, y } = localCoords(event)
      dragNode = nodeAt(x, y)
    }
    const onMouseMove = (event) => {
      if (!dragNode) return
      const { x, y } = localCoords(event)
      dragNode.x = x
      dragNode.y = y
    }
    const onMouseUp = async (event) => {
      const wasDrag = dragNode
      dragNode = null
      if (!wasDrag && event.type === 'mouseup') {
        const { x, y } = localCoords(event)
        const hit = nodeAt(x, y)
        selected.value = hit
        neighbors.value = null
        if (hit) {
          try {
            neighbors.value = await apiGet(`/api/literature/graph/neighbors/${encodeURIComponent(hit.rid)}`)
          } catch {
            neighbors.value = null
          }
        }
      }
    }

    const selectNode = async (node) => {
      selected.value = node
      neighbors.value = null
      if (!node) return
      try {
        neighbors.value = await apiGet(`/api/literature/graph/neighbors/${encodeURIComponent(node.rid)}`)
      } catch {
        neighbors.value = null
      }
    }

    onMounted(async () => {
      await load()
      tick()
    })
    onUnmounted(() => {
      if (raf) cancelAnimationFrame(raf)
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
    color: #fff;
    background: var(--main-color);
    border-color: var(--main-color);
  }
}
.limit-input {
  width: 80px;
  padding: 4px 8px;
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
  opacity: 0.8;
  margin: 2px 0 8px;
  word-break: break-all;
}
</style>
