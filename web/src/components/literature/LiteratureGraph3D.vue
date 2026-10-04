<template>
  <div
    ref="host"
    class="graph-3d"
    @pointermove="onPointerMove"
    @pointerdown="pointerStart = { x: $event.clientX, y: $event.clientY }"
    @pointerleave="clearHover"
    @click="onClick"
  >
    <canvas ref="labelsCanvas" class="labels-layer" aria-hidden="true"></canvas>
    <div v-if="unsupported" class="webgl-fallback">
      当前浏览器未启用 WebGL，请切回 2D 视图或启用硬件加速。
    </div>
    <div v-else-if="hovered" class="node-tooltip" :style="tooltipStyle">
      {{ hovered.title || hovered.rid }}
    </div>
  </div>
</template>

<script setup>
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  onActivated,
  onDeactivated,
  ref,
  watch
} from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js'
import { LineMaterial } from 'three/addons/lines/LineMaterial.js'
import { useThemeStore } from '@/stores/theme'
import {
  graphCameraPosition,
  graphNodeColor,
  graphNodeRadius,
  graphNodeLabel,
  graphStyleDefaults
} from '@/utils/literature_graph'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  selectedRid: { type: String, default: '' },
  styleOptions: { type: Object, default: graphStyleDefaults },
  positions: { type: Object, default: null },
  active: { type: Boolean, default: true }
})
const emit = defineEmits(['select'])

const themeStore = useThemeStore()
const host = ref(null)
const labelsCanvas = ref(null)
const unsupported = ref(false)
const hovered = ref(null)
const pointer = ref({ x: 0, y: 0 })
const tooltipStyle = computed(() => ({
  left: `${pointer.value.x + 14}px`,
  top: `${pointer.value.y + 14}px`
}))

let renderer
let scene
let camera
let controls
let nodeMesh
let edgeLines
let arrowMesh
let resizeObserver
let animationFrame
let nodeIndex = []
let raycaster
let disposed = false,
  userMoved = false,
  pointerStart = null
let nodePositions = new Map(),
  edgeIndex = []
const mouse = new THREE.Vector2(2, 2)
const matrix = new THREE.Matrix4()
const color = new THREE.Color()

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
  if (arrowMesh) {
    scene?.remove(arrowMesh)
    arrowMesh.geometry.dispose()
    arrowMesh.material.dispose()
    arrowMesh = null
  }
}

function buildGraph() {
  if (!scene) return
  disposeGraph()
  userMoved = false
  nodeIndex = props.nodes
  if (!nodeIndex.length) return

  nodePositions = new Map(nodeIndex.map((node) => [node.rid, new THREE.Vector3()]))

  const geometry = new THREE.SphereGeometry(1, 16, 12)
  const material = new THREE.MeshLambertMaterial({ vertexColors: true })
  nodeMesh = new THREE.InstancedMesh(geometry, material, nodeIndex.length)
  nodeMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
  scene.add(nodeMesh)

  edgeIndex = props.edges.filter(
    (edge) =>
      nodePositions.has(edge.source) &&
      nodePositions.has(edge.target) &&
      edge.source !== edge.target
  )
  const edgeGeometry = new LineSegmentsGeometry()
  edgeGeometry.setPositions(new Float32Array(edgeIndex.length * 6))
  edgeLines = new LineSegments2(
    edgeGeometry,
    new LineMaterial({
      color: props.styleOptions.edgeColor || 0x6b8bb7,
      transparent: true,
      opacity: 0.28
    })
  )
  edgeLines.frustumCulled = false
  scene.add(edgeLines)
  if (edgeIndex.length) {
    arrowMesh = new THREE.InstancedMesh(
      new THREE.ConeGeometry(1, 1, 6),
      new THREE.MeshBasicMaterial(),
      edgeIndex.length
    )
    arrowMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
    scene.add(arrowMesh)
  }
  updatePositions()
  applyStyle()
}

function updatePositions() {
  if (!nodeMesh || !props.positions || props.positions.length !== nodeIndex.length * 3) return
  nodeIndex.forEach((node, index) =>
    nodePositions.get(node.rid).set(...props.positions.subarray(index * 3, index * 3 + 3))
  )
  updateGeometry()
  if (!userMoved) fit()
}

function updateGeometry() {
  if (!nodeMesh) return
  nodeIndex.forEach((node, index) => {
    const radius = graphNodeRadius(node, props.styleOptions) * 0.5
    matrix.compose(
      nodePositions.get(node.rid),
      new THREE.Quaternion(),
      new THREE.Vector3(radius, radius, radius)
    )
    nodeMesh.setMatrixAt(index, matrix)
  })
  nodeMesh.instanceMatrix.needsUpdate = true
  nodeMesh.computeBoundingSphere()
  if (edgeIndex.length) {
    const vertices = edgeLines.geometry.attributes.instanceStart.data.array
    edgeIndex.forEach((edge, index) => {
      const start = nodePositions.get(edge.source),
        end = nodePositions.get(edge.target)
      vertices.set([start.x, start.y, start.z, end.x, end.y, end.z], index * 6)
      const direction = end.clone().sub(start)
      const length = direction.length()
      const width = Math.max(0.5, Number(props.styleOptions.edgeWidth) || 0)
      matrix.compose(
        start.clone().lerp(end, 0.86),
        new THREE.Quaternion().setFromUnitVectors(
          new THREE.Vector3(0, 1, 0),
          length ? direction.divideScalar(length) : new THREE.Vector3(0, 1, 0)
        ),
        new THREE.Vector3(width, Math.min(length * 0.12, width * 3), width)
      )
      arrowMesh.setMatrixAt(index, matrix)
    })
    edgeLines.geometry.attributes.instanceStart.data.needsUpdate = true
    arrowMesh.instanceMatrix.needsUpdate = true
    arrowMesh.computeBoundingSphere()
  }
}

function applyStyle() {
  if (!scene) return
  const background = props.styleOptions.background
  scene.background =
    background === 'theme'
      ? null
      : new THREE.Color(
          background === 'dark'
            ? '#111827'
            : background === 'light'
              ? '#ffffff'
              : props.styleOptions.bgColor
        )
  applySelection()
  updateGeometry()
  if (edgeLines) {
    const width = Math.max(0, Math.min(5, Number(props.styleOptions.edgeWidth) || 0))
    edgeLines.visible = !!props.styleOptions.showEdges && width > 0
    edgeLines.material.linewidth = width
    edgeLines.material.color.set(props.styleOptions.edgeColor || '#6b8bb7')
    edgeLines.material.resolution.set(host.value.clientWidth, host.value.clientHeight)
    if (arrowMesh) {
      arrowMesh.visible = edgeLines.visible && !!props.styleOptions.showEdgeDir
      arrowMesh.material.color.copy(edgeLines.material.color)
    }
  }
}

function applySelection() {
  if (!nodeMesh) return
  nodeIndex.forEach((node, index) => {
    const selected = node.rid === props.selectedRid
    nodeMesh.setColorAt(
      index,
      color.set(selected ? '#f5222d' : graphNodeColor(node, props.styleOptions, themeStore.isDark))
    )
  })
  nodeMesh.instanceColor.needsUpdate = true
}

function fit(direction) {
  if (!camera || !controls) return
  const bounds = new THREE.Box3().setFromPoints([...nodePositions.values()])
  const center = bounds.isEmpty() ? new THREE.Vector3() : bounds.getCenter(new THREE.Vector3())
  const size = bounds.isEmpty() ? 0 : bounds.getSize(new THREE.Vector3()).length()
  const distance = Math.max(
    120,
    (size * 0.65) / Math.tan((camera.fov * Math.PI) / 360) / Math.min(1, camera.aspect)
  )
  const offset = new THREE.Vector3(...graphCameraPosition(direction))
    .normalize()
    .multiplyScalar(distance)
  camera.position.copy(center.clone().add(offset))
  controls.target.copy(center)
  camera.far = Math.max(3000, distance * 10)
  controls.maxDistance = camera.far / 2
  camera.updateProjectionMatrix()
  controls.update()
}

function locate(rid) {
  userMoved = true
  const index = nodeIndex.findIndex((node) => node.rid === rid)
  if (index < 0 || !nodeMesh || !camera || !controls) return
  const transform = new THREE.Matrix4()
  nodeMesh.getMatrixAt(index, transform)
  const target = new THREE.Vector3().setFromMatrixPosition(transform)
  const offset = camera.position.clone().sub(controls.target).normalize().multiplyScalar(120)
  controls.target.copy(target)
  camera.position.copy(target.clone().add(offset))
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
  if (unsupported.value || !raycaster || !camera) return
  if (
    pointerStart &&
    Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 4
  )
    return
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
  if (labelsCanvas.value) {
    labelsCanvas.value.width = width
    labelsCanvas.value.height = height
  }
  edgeLines?.material.resolution.set(width, height)
}

function animate() {
  if (disposed || !props.active || !renderer) {
    animationFrame = null
    return
  }
  controls?.update()
  renderer?.render(scene, camera)
  const ctx = labelsCanvas.value?.getContext('2d')
  if (ctx) {
    ctx.clearRect(0, 0, labelsCanvas.value.width, labelsCanvas.value.height)
    ctx.font = '12px sans-serif'
    const dark =
      props.styleOptions.background === 'dark' ||
      (props.styleOptions.background === 'theme' && themeStore.isDark)
    ctx.fillStyle = dark ? '#eeeeee' : '#222222'
    for (const node of nodeIndex) {
      const label = graphNodeLabel(node, props.styleOptions.labelMode, props.selectedRid)
      if (!label) continue
      const p = nodePositions.get(node.rid).clone().project(camera)
      if (Math.abs(p.x) <= 1 && Math.abs(p.y) <= 1 && Math.abs(p.z) <= 1)
        ctx.fillText(
          label,
          ((p.x + 1) * labelsCanvas.value.width) / 2 + 8,
          ((1 - p.y) * labelsCanvas.value.height) / 2
        )
    }
  }
  animationFrame = requestAnimationFrame(animate)
}

onMounted(async () => {
  await nextTick()
  if (disposed) return
  try {
    renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance'
    })
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
  controls.addEventListener('start', () => {
    userMoved = true
  })
  controls.enabled = props.active
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(host.value)
  resize()
  buildGraph()
  if (props.active) animate()
})

watch(() => [props.nodes, props.edges], buildGraph, { deep: true })
watch(() => props.selectedRid, applySelection)
watch(() => themeStore.isDark, applySelection)
watch(() => props.styleOptions, applyStyle, { deep: true })
watch(() => props.positions, updatePositions)
const stopAnimation = () => {
  cancelAnimationFrame(animationFrame)
  animationFrame = null
}
watch(
  () => props.active,
  (active) => {
    if (controls) controls.enabled = active
    stopAnimation()
    if (active && renderer) {
      resize()
      animate()
    }
  }
)
onDeactivated(stopAnimation)
onActivated(() => {
  if (props.active && renderer && !animationFrame) animate()
})

onBeforeUnmount(() => {
  disposed = true
  cancelAnimationFrame(animationFrame)
  resizeObserver?.disconnect()
  controls?.dispose()
  disposeGraph()
  renderer?.dispose()
  renderer?.domElement?.remove()
})

defineExpose({ fit, locate })
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
.labels-layer {
  position: absolute;
  inset: 0;
  z-index: 1;
  pointer-events: none;
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
