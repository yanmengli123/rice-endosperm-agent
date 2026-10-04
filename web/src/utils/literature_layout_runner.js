export function createLiteratureLayoutRunner(
  onUpdate,
  onError,
  factory = () =>
    new Worker(new URL('../workers/literature_layout.worker.js', import.meta.url), {
      type: 'module'
    })
) {
  let worker,
    generation = 0
  const stop = () => {
    generation++
    worker?.terminate()
    worker = null
  }
  return {
    stop,
    start(nodes, edges, options) {
      stop()
      const ticket = generation,
        count = nodes.length
      try {
        worker = factory()
        worker.onmessage = ({ data }) => {
          if (ticket !== generation) return
          if (data.error) {
            stop()
            onError(data.error)
            return
          }
          if (
            !(data.positions instanceof Float32Array) ||
            data.positions.length !== count * 3 ||
            !data.positions.every(Number.isFinite)
          ) {
            stop()
            onError('布局响应坐标损坏，保留上一份画布')
            return
          }
          onUpdate(data)
          if (data.done) stop()
        }
        worker.onerror = () => {
          if (ticket !== generation) return
          stop()
          onError('布局 worker 执行失败；可重算或切换几何布局')
        }
        worker.postMessage({
          // Send only numeric-layout inputs, never nested reactive metadata.
          nodes: nodes.map((node) => ({
            rid: node.rid,
            citations: node.citations,
            layoutX: node.layoutX,
            layoutY: node.layoutY,
            layoutZ: node.layoutZ
          })),
          edges: edges.map((edge) => ({ source: edge.source, target: edge.target })),
          options: {
            algorithm: options?.algorithm,
            iterations: options?.iterations,
            scaling: options?.scaling,
            gravity: options?.gravity,
            spacing: options?.spacing,
            style: { scaleAlgo: options?.style?.scaleAlgo, sizeBase: options?.style?.sizeBase }
          }
        })
      } catch (error) {
        stop()
        onError(error.message || '浏览器不支持布局 worker')
      }
    }
  }
}
