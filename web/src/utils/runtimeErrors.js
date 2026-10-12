// Runtime diagnostics contain categories only: never retain exception text,
// stacks, request URLs, model prompts, credentials or Promise rejection objects.
const ERROR_NAMES = new Set([
  'Error',
  'TypeError',
  'RangeError',
  'ReferenceError',
  'SyntaxError',
  'URIError',
  'EvalError',
  'AbortError',
  'TimeoutError'
])
const PHASES = new Set([
  'setup function',
  'render function',
  'watcher getter',
  'watcher callback',
  'watcher cleanup function',
  'native event handler',
  'component event handler',
  'mounted hook',
  'updated hook',
  'beforeUnmount hook',
  'unmounted hook'
])

export function installRuntimeErrorHandling(
  app,
  { target = window, notify, development = false, logger = console, now = Date.now } = {}
) {
  const recent = []
  let lastNotice = -Infinity
  const report = (error, source, info) => {
    let name = 'UnknownError'
    try {
      if (ERROR_NAMES.has(error?.name)) name = error.name
    } catch {
      // Hostile rejection values can even have throwing property accessors.
    }
    const entry = Object.freeze({
      time: new Date(now()).toISOString(),
      source,
      category: name,
      phase: PHASES.has(info) ? info : 'unknown'
    })
    recent.push(entry)
    if (recent.length > 20) recent.shift()
    // Deliberately do not log the raw Error even in development.
    if (development) {
      try {
        logger.error('[yuxi:runtime-error]', entry)
      } catch {
        /* diagnostic only */
      }
    }
    if (now() - lastNotice < 5000) return
    lastNotice = now()
    try {
      notify?.('界面出现了一个错误，部分功能可能受影响；请重试或刷新页面。')
    } catch {
      /* notification failures must not recurse */
    }
  }
  const vueHandler = (error, _instance, info) => report(error, 'vue', info)
  const rejectionHandler = (event) => {
    // Suppress the browser's default raw rejection log after recording it safely.
    event.preventDefault()
    report(event.reason, 'promise', 'unknown')
  }
  const previous = app.config.errorHandler
  app.config.errorHandler = vueHandler
  target.addEventListener('unhandledrejection', rejectionHandler)
  Object.defineProperty(target, '__yuxiRecentErrors', {
    configurable: true,
    get: () => recent.slice()
  })
  return () => {
    target.removeEventListener('unhandledrejection', rejectionHandler)
    if (app.config.errorHandler === vueHandler) app.config.errorHandler = previous
    delete target.__yuxiRecentErrors
  }
}
