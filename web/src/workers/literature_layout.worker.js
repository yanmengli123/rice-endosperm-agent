import { createLiteratureLayout } from '../utils/literature_layout_engine.js'

let generation = 0
globalThis.onmessage = ({ data }) => {
  const ticket = ++generation
  let engine
  try {
    engine = createLiteratureLayout(data.nodes, data.edges, data.options)
  } catch (error) {
    globalThis.postMessage({ error: error.message })
    return
  }
  const start = performance.now()
  const iterations = Math.max(10, Math.min(1000, Number(data.options?.iterations) || 300))
  let completed = 0
  const run = () => {
    if (ticket !== generation) {
      engine.dispose()
      return
    }
    try {
      const positions = engine.tick(Math.min(10, iterations - completed))
      completed += Math.min(10, iterations - completed)
      const timedOut = performance.now() - start > 15000
      const done = completed >= iterations || engine.settled() || timedOut
      globalThis.postMessage(
        { positions, completed, done, reason: timedOut ? 'budget' : 'complete' },
        [positions.buffer]
      )
      if (done) engine.dispose()
      else setTimeout(run, 0)
    } catch (error) {
      engine.dispose()
      globalThis.postMessage({ error: error.message })
    }
  }
  run()
}
