// 全局单一任务轮询源（企业级常驻数据层）：
// 状态栏 / 任务视图 / 导入面板共享同一份 5s 轮询，避免 N 个消费者 N 份请求。
// 订阅计数归零或页面不可见（visibilitychange）时自动暂停，回到前台立即补拉。
// 阶段真相只来自后端投影（status/stage/is_* 布尔位），前端不做状态推断。
import { readonly, ref } from 'vue'
import { getCurrentScope, onScopeDispose } from 'vue'
import { listLiteratureTasks } from '@/apis/literature_api'

const TASK_FEED_MS = 5000

// 模块级单例：同一页面无论多少组件消费，只跑一个 interval。
const tasks = ref([])
const updatedAt = ref(0)
const feedError = ref('')

let subscribers = 0
let timer = null
let inFlight = false

export async function refreshLiteratureTasks() {
  if (inFlight) return
  inFlight = true
  try {
    const data = await listLiteratureTasks(100)
    tasks.value = data?.tasks || []
    updatedAt.value = Date.now()
    feedError.value = ''
  } catch (error) {
    feedError.value = error?.message || String(error)
  } finally {
    inFlight = false
  }
}

function shouldRun() {
  return subscribers > 0 && typeof document !== 'undefined' && !document.hidden
}

function syncTimer() {
  if (shouldRun() && !timer) {
    timer = window.setInterval(refreshLiteratureTasks, TASK_FEED_MS)
    refreshLiteratureTasks()
  } else if (!shouldRun() && timer) {
    window.clearInterval(timer)
    timer = null
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', syncTimer)
}

export function useLiteratureTaskFeed() {
  subscribers += 1
  syncTimer()
  // 组件作用域内自动退订（keepAlive 页面 onScopeDispose 在真正卸载时触发）
  if (getCurrentScope()) {
    onScopeDispose(() => {
      subscribers = Math.max(0, subscribers - 1)
      syncTimer()
    })
  }
  return {
    tasks: readonly(tasks),
    updatedAt: readonly(updatedAt),
    feedError: readonly(feedError),
    refresh: refreshLiteratureTasks
  }
}
