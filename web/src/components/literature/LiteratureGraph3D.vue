<template>
  <div ref="host" class="graph-3d" @pointermove="onPointerMove" @pointerleave="clearHover" @click="onClick">
    <div v-if="unsupported" class="webgl-fallback">
      当前浏览器未启用 WebGL，请切回 2D 视图或启用硬件加速。
    </div>
    <div v-else-if="hovered" class="node-tooltip" :style="tooltipStyle">
      {{ hovered.title || hovered.rid }}
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { useThemeStore } from '@/stores/theme'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  selectedRid: { type: String, default: '' }
})
const emit = defineEmits(['select'])

const COLORS_LIGHT = { Q1: 0x1677ff, Q2: 0x52c41a, Q3: 0xfaad14, Q4: 0x8c8c8c }
const COLORS_DARK = { Q1: 0x69b1ff, Q2: 0x95de64, Q3: 0xffd666, Q4: 0xa6a6a6 }
const themeStore = useThemeStore()
const COLORS = computed(() => (themeStore.isDark ? COLORS_DARK : COLORS_LIGHT))
const host = ref(null)
const unsupported = ref(false)
const hovered = ref(null)
const pointer = ref({ x: 0, y: 0 })
const tooltipStyle = computed(() => ({ left: `${pointer.value.x + 14}px`, top: `${pointer.value.y + 14}px` }))

let renderer
let scene
let camera
let controls
let nodeMesh
let edgeLines
let resizeObserver
let animationFrame
let nodeIndex = []
let raycaster
const mouse = new THREE.Vector2(2, 2)
const matrix = new THREE.Matrix4()
const color = new THREE.Color()

function deterministicPosition(node, index, count) {
  // Fibonacci sphere gives a stable, genuinely three-dimensional starting layout.
  // Citation count pulls important papers closer to the readable foreground shell.
  const offset = 2 / Math.max(1, count)
  const y = index * offset - 1 + offset / 2
  const radius = Math.sqrt(Math.max(0, 1 - y * y))
  const phi = index * Math.PI * (3 - Math.sqrt(5))
  const shell = 95 + Math.min(80, Math.log2(2 + Number(node.citations || 0)) * 10)
  return new THREE.Vector3(Math.cos(phi) * radius * shell, y * shell, Math.sin(phi) * radius * shell)
}

function disposeGraph() {
  if (nodeMesh) {
    scene?.remove(nodeMesh)
    nodeMesh.geometry.dispose()
    nodeMesh.material.dispose()
    nodeMesh = null
  }
  if (edgeLines) {
    scene?.remove(edgeLines)
    edgeLines.geometry.dispose()
    edgeLines.material.dispose()
    edgeLines = null
  }
}

function buildGraph() {
  if (!scene) return
  disposeGraph()
  nodeIndex = props.nodes
  if (!nodeIndex.length) return

  const positions = new Map()
  nodeIndex.forEach((node, index) => positions.set(node.rid, deterministicPosition(node, index, nodeIndex.length)))

  const geometry = new THREE.SphereGeometry(1, 16, 12)
  const material = new THREE.MeshLambertMaterial({ vertexColors: true })
  nodeMesh = new THREE.InstancedMesh(geometry, material, nodeIndex.length)
  nodeMesh.instanceMatrix.setUsage(THREE.StaticDrawUsage)
  nodeIndex.forEach((node, index) => {
    const p = positions.get(node.rid)
    const scale = 2.2 + Math.min(7, Math.sqrt(Number(node.citations || 0)) * 0.8)
    matrix.compose(p, new THREE.Quaternion(), new THREE.Vector3(scale, scale, scale))
    nodeMesh.setMatrixAt(index, matrix)
    nodeMesh.setColorAt(index, color.setHex(COLORS.value[node.quartile] || 0xb8c1cc))
  })
  nodeMesh.instanceMatrix.needsUpdate = true
  nodeMesh.instanceColor.needsUpdate = true
  scene.add(nodeMesh)

  const edgeVertices = []
  props.edges.forEach((edge) => {
    const source = positions.get(edge.source)
    const target = positions.get(edge.target)
    if (source && target) edgeVertices.push(source.x, source.y, source.z, target.x, target.y, target.z)
  })
  const edgeGeometry = new THREE.BufferGeometry()
  edgeGeometry.setAttribute('position', new THREE.Float32BufferAttribute(edgeVertices, 3))
  edgeLines = new THREE.LineSegments(
    edgeGeometry,
    new THREE.LineBasicMaterial({ color: 0x6b8bb7, transparent: true, opacity: 0.28 })
  )
  scene.add(edgeLines)
  applySelection()
  fit()
}

function applySelection() {
  if (!nodeMesh) return
  nodeIndex.forEach((node, index) => {
    const selected = node.rid === props.selectedRid
    nodeMesh.setColorAt(index, color.setHex(selected ? 0xf5222d : COLORS.value[node.quartile] || 0xb8c1cc))
  })
  nodeMesh.instanceColor.needsUpdate = true
}

function fit() {
  if (!camera || !controls) return
  camera.position.set(0, 75, 310)
  controls.target.set(0, 0, 0)
  controls.update()
}

function updatePointer(event) {
  const rect = host.value.getBoundingClientRect()
  pointer.value = { x: event.clientX - rect.left, y: event.clientY - rect.top }
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
}

function pick() {
  if (!nodeMesh) return null
  raycaster.setFromCamera(mouse, camera)
  const hit = raycaster.intersectObject(nodeMesh, false)[0]
  return hit?.instanceId == null ? null : nodeIndex[hit.instanceId]
}

function onPointerMove(event) {
  if (unsupported.value) return
  updatePointer(event)
  hovered.value = pick()
  if (renderer?.domElement) renderer.domElement.style.cursor = hovered.value ? 'pointer' : 'grab'
}

function clearHover() {
  hovered.value = null
  mouse.set(2, 2)
}

function onClick(event) {
  updatePointer(event)
  const node = pick()
  if (node) emit('select', node)
}

function resize() {
  if (!renderer || !camera || !host.value) return
  const width = Math.max(1, host.value.clientWidth)
  const height = Math.max(1, host.value.clientHeight)
  renderer.setSize(width, height, false)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  camera.aspect = width / height
  camera.updateProjectionMatrix()
}

function animate() {
  controls?.update()
  renderer?.render(scene, camera)
  animationFrame = requestAnimationFrame(animate)
}

onMounted(async () => {
  await nextTick()
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' })
  } catch {
    unsupported.value = true
    return
  }
  renderer.setClearColor(0x000000, 0)
  host.value.appendChild(renderer.domElement)
  scene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(55, 1, 0.1, 3000)
  raycaster = new THREE.Raycaster()
  scene.add(new THREE.AmbientLight(0xffffff, 1.5))
  const light = new THREE.DirectionalLight(0xffffff, 2.2)
  light.position.set(120, 180, 240)
  scene.add(light)
  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.08
  controls.minDistance = 45
  controls.maxDistance = 1100
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(host.value)
  resize()
  buildGraph()
  animate()
})

watch(() => [props.nodes, props.edges], buildGraph, { deep: true })
watch(() => props.selectedRid, applySelection)

onBeforeUnmount(() => {
  cancelAnimationFrame(animationFrame)
  resizeObserver?.disconnect()
  controls?.dispose()
  disposeGraph()
  renderer?.dispose()
  renderer?.domElement?.remove()
})

defineExpose({ fit })
</script>

<style scoped>
.graph-3d {
  position: relative;
  width: 100%;
  height: 560px;
  overflow: hidden;
  background: radial-gradient(circle at 50% 45%, rgba(53, 104, 178, 0.1), transparent 68%);
}
.graph-3d :deep(canvas) {
  display: block;
  width: 100%;
  height: 100%;
}
.node-tooltip {
  position: absolute;
  z-index: 2;
  max-width: 320px;
  padding: 6px 9px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--component-background, rgba(255, 255, 255, 0.94));
  font-size: 12px;
  pointer-events: none;
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.3);
}
.webgl-fallback {
  display: grid;
  height: 100%;
  place-items: center;
  color: var(--text-color-secondary, #666);
}
</style>
