import { DirectedGraph } from 'graphology'
import forceAtlas2 from 'graphology-layout-forceatlas2'
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  forceX,
  forceY,
  forceZ
} from 'd3-force-3d'
import { graphGeometry, graphNodeRadius } from './literature_graph.js'

// This is shared by the module worker and numerical tests, not a renderer.
export function createLiteratureLayout(nodes, edges, options = {}) {
  const algorithm = options.algorithm || 'fa2'
  if (!['fa2', 'force3d'].includes(algorithm)) throw Error('不支持的力导向算法')
  if (nodes.length > 5000 || new Set(nodes.map((node) => node.rid)).size !== nodes.length)
    throw Error('图谱节点超限或标识重复')
  const bounded = (value, fallback) =>
    Number.isFinite(Number(value)) && Number(value) > 0
      ? Math.max(0.1, Math.min(10, Number(value)))
      : fallback
  const scaling = bounded(options.scaling, 1),
    gravity = bounded(options.gravity, 1),
    spacing = bounded(options.spacing, 1)
  const initial = graphGeometry(nodes, 'random', spacing).map((node, index) => ({
    ...node,
    x: Number.isFinite(node.layoutX) ? node.layoutX : node.x - 450,
    y: Number.isFinite(node.layoutY) ? node.layoutY : node.y - 280,
    z: Number.isFinite(node.layoutZ) ? node.layoutZ : Math.sin(index * 2.399963) * 70,
    size: graphNodeRadius(node, options.style) * spacing
  }))
  const ids = new Set(initial.map((node) => node.rid))
  const links = edges
    .filter((edge) => edge.source !== edge.target && ids.has(edge.source) && ids.has(edge.target))
    .map((edge) => ({ source: edge.source, target: edge.target }))
  let graph, simulation
  if (algorithm === 'fa2') {
    graph = new DirectedGraph({ allowSelfLoops: false })
    for (const node of initial) graph.addNode(node.rid, { x: node.x, y: node.y, size: node.size })
    for (const edge of links) {
      if (!graph.hasDirectedEdge(edge.source, edge.target))
        graph.addDirectedEdge(edge.source, edge.target)
    }
  } else {
    simulation = forceSimulation(initial, 3)
      .stop()
      .force(
        'charge',
        forceManyBody()
          .strength(-70 * scaling)
          .distanceMin(1)
      )
      .force(
        'link',
        forceLink(links)
          .id((node) => node.rid)
          .distance(40 + spacing * 15)
          .strength(0.2)
      )
      .force('center', forceCenter(0, 0, 0))
      .force('collision', forceCollide((node) => node.size).iterations(2))
      .force('x', forceX(0).strength(0.005 * gravity))
      .force('y', forceY(0).strength(0.005 * gravity))
      .force('z', forceZ(0).strength(0.005 * gravity))
  }
  const positions = () => {
    const result = new Float32Array(initial.length * 3)
    initial.forEach((node, index) => {
      const position = graph ? graph.getNodeAttributes(node.rid) : node
      result.set([position.x, position.y, graph ? 0 : position.z], index * 3)
    })
    if (!result.every(Number.isFinite)) throw Error('力导向产生非法坐标，未替换当前布局')
    return result
  }
  return {
    positions,
    tick(iterations = 10) {
      if (initial.length < 2) return positions()
      if (graph)
        forceAtlas2.assign(graph, {
          iterations,
          settings: {
            barnesHutOptimize: true,
            adjustSizes: true,
            scalingRatio: 30 * scaling,
            gravity: 0.8 * gravity,
            strongGravityMode: false,
            slowDown: 2
          }
        })
      else simulation.tick(iterations)
      return positions()
    },
    settled: () => initial.length < 2 || (simulation && simulation.alpha() < 0.005),
    dispose: () => {
      simulation?.stop()
      graph?.clear()
    }
  }
}
