// Literature session list state, shared by the pinned sidebar and the session
// workspace so both surfaces operate on one selection model.
// PaperAgent §6.4 parity: row delete, batch delete, purge archived.
import { computed, ref } from 'vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'

export function useLiteratureSessions() {
  const sessions = ref([])
  const selectedId = ref('')
  const selectedIds = ref([])
  const archived = ref(false)
  const busy = ref(false)
  const notice = ref('')
  let disposed = false
  let generation = 0

  const allSelected = computed(
    () =>
      sessions.value.length > 0 &&
      sessions.value.every((item) => selectedIds.value.includes(item.id))
  )
  const forget = (id) => {
    selectedIds.value = selectedIds.value.filter((item) => item !== id)
    if (selectedId.value === id) selectedId.value = ''
  }
  const perform = async (action) => {
    if (busy.value || disposed) return
    busy.value = true
    notice.value = ''
    try {
      await action()
    } catch (error) {
      if (!disposed) notice.value = error?.message || '会话操作失败'
    } finally {
      busy.value = false
    }
  }
  const refresh = async () => {
    if (disposed) return
    const ticket = ++generation
    const requested = archived.value
    try {
      const data = await api.sessions(requested)
      if (disposed || ticket !== generation) return
      sessions.value = data?.items || []
      const visible = new Set(sessions.value.map((item) => item.id))
      selectedIds.value = selectedIds.value.filter((id) => visible.has(id))
      if (archived.value || !visible.has(selectedId.value)) selectedId.value = ''
    } catch (error) {
      if (!disposed && ticket === generation) notice.value = error?.message || '会话载入失败'
    }
  }
  const toggleAll = (checked) => {
    if (busy.value || disposed) return
    selectedIds.value = checked ? sessions.value.map((session) => session.id) : []
  }
  const toggleOne = (id, checked) => {
    if (busy.value || disposed || !sessions.value.some((item) => item.id === id)) return
    selectedIds.value = checked
      ? [...new Set([...selectedIds.value, id])]
      : selectedIds.value.filter((item) => item !== id)
  }
  const setArchived = async (value) => {
    if (busy.value || disposed) return
    archived.value = !!value
    selectedIds.value = []
    selectedId.value = ''
    await refresh()
  }
  /**
   * Soft delete: the server re-checks ownership and flips status to deleted, so
   * message history and usage records survive. Each id is attempted separately and
   * partial failures are reported instead of being masked as an all-success run.
   */
  const removeIds = async (ids, confirmText) => {
    const visible = new Set(sessions.value.map((item) => item.id))
    ids = [...new Set(ids)].filter((id) => visible.has(id))
    if (!ids.length) return
    if (typeof window !== 'undefined' && !window.confirm(confirmText)) return
    let removed = 0
    const failures = []
    for (const id of ids) {
      if (disposed) break
      try {
        await api.deleteSession(id)
        forget(id)
        removed += 1
      } catch (error) {
        failures.push(error?.message || id)
      }
    }
    notice.value = failures.length
      ? `已删除 ${removed} 个会话；${failures.length} 个失败：${failures.join('、')}`
      : `已删除 ${removed} 个会话（历史与用量记录保留）`
    await refresh()
  }
  const removeOne = (id) =>
    perform(() => removeIds([id], '删除该会话？历史消息与用量记录会保留，可重新新建。'))
  const deleteSelected = () =>
    perform(() =>
      removeIds(
        [...selectedIds.value],
        `删除选中的 ${selectedIds.value.length} 个会话？历史消息与用量记录会保留。`
      )
    )
  const clearArchived = () => {
    if (!archived.value) return
    return perform(() =>
      removeIds(
        sessions.value.map((session) => session.id),
        `清空全部 ${sessions.value.length} 个已归档会话？历史消息与用量记录会保留。`
      )
    )
  }
  const restore = (id) =>
    perform(async () => {
      await api.updateSession(id, { status: 'active' })
      await refresh()
    })
  const dispose = () => {
    disposed = true
    generation++
  }
  return {
    sessions,
    selectedId,
    selectedIds,
    archived,
    busy,
    notice,
    allSelected,
    refresh,
    perform,
    toggleAll,
    toggleOne,
    setArchived,
    removeOne,
    deleteSelected,
    clearArchived,
    restore,
    dispose
  }
}
