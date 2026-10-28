// 全局单一任务轮询源（企业级常驻数据层）：
// 状态栏 / 任务视图 / 导入面板共享同一份 5s 轮询，避免 N 个消费者 N 份请求。
// 订阅计数归零或页面不可见（visibilitychange）时自动暂停，回到前台立即补拉。
// 阶段真相只来自后端投影（status/stage/is_* 布尔位），前端不做状态推断。
import { readonly, ref, watch, onActivated, onDeactivated, onScopeDispose } from 'vue'
import { listLiteratureTasks } from '@/apis/literature_api'
import { useUserStore } from '@/stores/user'

const TASK_FEED_MS = 5000

// 模块级单例：同一页面无论多少组件消费，只跑一个 interval。
const tasks = ref([])
const updatedAt = ref(0)
const feedError = ref('')
const ownerKey = ref('')

let subscribers = 0
let consumers = 0
let timer = null
let request = null

export async function refreshLiteratureTasks() {
  if (!shouldRun() || request) return
  const current = new AbortController()
  request = current
  try {
    const data = await listLiteratureTasks(100, current.signal)
    if (request !== current || current.signal.aborted) return
    tasks.value = data?.tasks || []
    updatedAt.value = Date.now()
    feedError.value = ''
  } catch (error) {
    if (request === current && !current.signal.aborted)
      feedError.value = `任务状态已过期，更新失败：${error?.message || String(error)}；将自动重试`
  } finally {
    if (request === current) request = null
  }
}

function shouldRun() {
  return ownerKey.value && subscribers > 0 && typeof document !== 'undefined' && !document.hidden
}

function stopPolling() {
  if (timer) window.clearInterval(timer)
  timer = null
  request?.abort()
  request = null
}

function syncTimer() {
  if (shouldRun() && !timer) {
    timer = window.setInterval(refreshLiteratureTasks, TASK_FEED_MS)
    refreshLiteratureTasks()
  } else if (!shouldRun()) stopPolling()
}

export function useLiteratureTaskFeed() {
  const userStore = useUserStore()
  let active = true
  consumers += 1
  subscribers += 1
  if (consumers === 1 && typeof document !== 'undefined')
    document.addEventListener('visibilitychange', syncTimer)
  // Pinia 归属来自已认证用户；停用中的 KeepAlive 消费者也必须清空旧账号快照。
  watch(
    () =>
      userStore.isLoggedIn && userStore.uid
        ? `${userStore.uid}:${userStore.departmentId ?? ''}`
        : '',
    (nextOwner) => {
      if (nextOwner !== ownerKey.value) {
        stopPolling()
        ownerKey.value = nextOwner
        tasks.value = []
        updatedAt.value = 0
        feedError.value = ''
      }
      syncTimer()
    },
    { immediate: true, flush: 'sync' }
  )
  const setActive = (next) => {
    if (active === next) return
    active = next
    subscribers += next ? 1 : -1
    syncTimer()
  }
  onActivated(() => setActive(true))
  onDeactivated(() => setActive(false))
  onScopeDispose(() => {
    setActive(false)
    consumers -= 1
    if (consumers === 0) {
      ownerKey.value = ''
      tasks.value = []
      updatedAt.value = 0
      feedError.value = ''
      if (typeof document !== 'undefined')
        document.removeEventListener('visibilitychange', syncTimer)
    }
  })
  return {
    tasks: readonly(tasks),
    updatedAt: readonly(updatedAt),
    feedError: readonly(feedError),
    ownerKey: readonly(ownerKey),
    refresh: refreshLiteratureTasks
  }
}
