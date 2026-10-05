// KeepAlive 停用与后台标签页不是卸载；阅读工作台的独立轮询统一遵守这一边界。
import { computed, ref, watch, onMounted, onActivated, onDeactivated, onBeforeUnmount } from 'vue'

export function useLiteraturePollingScope(pause, resume) {
  const routeActive = ref(true)
  const visible = ref(typeof document === 'undefined' || !document.hidden)
  const active = computed(() => routeActive.value && visible.value)
  const visibilityChanged = () => {
    visible.value = !document.hidden
  }
  watch(
    active,
    (value) => {
      if (value) resume()
      else pause()
    },
    { flush: 'sync' }
  )
  onMounted(() => {
    if (typeof document !== 'undefined')
      document.addEventListener('visibilitychange', visibilityChanged)
  })
  onActivated(() => {
    routeActive.value = true
  })
  onDeactivated(() => {
    routeActive.value = false
  })
  onBeforeUnmount(() => {
    routeActive.value = false
    if (typeof document !== 'undefined')
      document.removeEventListener('visibilitychange', visibilityChanged)
  })
  return active
}
