// Literature session list state, shared by the pinned sidebar and the session
// workspace so both surfaces operate on one selection model.
// PaperAgent §6.4 parity: row delete, batch delete, purge archived.
import { computed, ref, watch } from 'vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
import { useUserStore } from '@/stores/user'
import { confirmAction } from './useConfirmAction'

const PAGE_SIZE = 50

// 危险操作确认原语已迁移到通用模块；此处再导出保持文献域既有 import 不变。
export { confirmAction }

export function useLiteratureSessions() {
  const sessions = ref([])
  const selectedId = ref('')
  const selectedIds = ref([])
  const archived = ref(false)
  const busy = ref(false)
  const notice = ref('')
  const searchText = ref('')
  const hasMore = ref(false)
  let nextOffset = 0
  let disposed = false
  let generation = 0
  let ownerGeneration = 0
  const user = useUserStore()
  const stopOwnerWatch = watch(
    () => JSON.stringify([user.uid, user.departmentId, user.isLoggedIn]),
    () => {
      ownerGeneration++
      generation++
      sessions.value = []
      selectedId.value = ''
      selectedIds.value = []
      archived.value = false
      busy.value = false
      notice.value = ''
      searchText.value = ''
      hasMore.value = false
      nextOffset = 0
    },
    { flush: 'sync' }
  )

  const allSelected = computed(
    () =>
      sessions.value.length > 0 &&
      sessions.value.every((item) => selectedIds.value.includes(item.id))
  )
  const forget = (id) => {
    selectedIds.value = selectedIds.value.filter((item) => item !== id)
    if (selectedId.value === id) selectedId.value = ''
  }
  const captureScope = () => {
    const owner = ownerGeneration
    return () => !disposed && owner === ownerGeneration
  }
  const perform = async (action) => {
    if (busy.value || disposed) return
    busy.value = true
    notice.value = ''
    const stillCurrent = captureScope()
    try {
      await action(stillCurrent)
    } catch (error) {
      if (stillCurrent()) notice.value = error?.message || '会话操作失败'
    } finally {
      if (stillCurrent()) busy.value = false
    }
  }
  // 搜索模式命中会话全文（join 消息），同一会话可能多行——按 id 去重保留首条（排序即相关度）。
  const applyPage = (data, append) => {
    const items = data?.items || []
    const seen = new Set(append ? sessions.value.map((item) => item.id) : [])
    const merged = append ? [...sessions.value] : []
    for (const item of items) {
      if (seen.has(item.id)) continue
      seen.add(item.id)
      merged.push(item)
    }
    sessions.value = merged
    hasMore.value = Boolean(data?.has_more)
    nextOffset = data?.next_offset ?? merged.length
    const visible = new Set(merged.map((item) => item.id))
    selectedIds.value = selectedIds.value.filter((id) => visible.has(id))
    if (archived.value || !visible.has(selectedId.value)) selectedId.value = ''
  }
  const requestPage = async (offset) => {
    const query = searchText.value.trim()
    return query
      ? api.searchSessions(query, { archived: archived.value, offset })
      : api.sessions(archived.value, offset, PAGE_SIZE)
  }
  const refresh = async () => {
    if (disposed) return
    const ticket = ++generation
    try {
      const data = await requestPage(0)
      if (disposed || ticket !== generation) return
      applyPage(data, false)
    } catch (error) {
      if (!disposed && ticket === generation) notice.value = error?.message || '会话载入失败'
    }
  }
  const loadMore = () =>
    perform(async () => {
      const ticket = generation
      const data = await requestPage(nextOffset)
      if (disposed || ticket !== generation) return
      applyPage(data, true)
    })
  const runSearch = () => {
    if (busy.value || disposed) return
    void refresh()
  }
  const clearSearch = () => {
    if (busy.value || disposed || !searchText.value) return
    searchText.value = ''
    void refresh()
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
  const removeIds = async (ids, confirmText, stillCurrent) => {
    const visible = new Set(sessions.value.map((item) => item.id))
    ids = [...new Set(ids)].filter((id) => visible.has(id))
    if (!ids.length) return
    if (!(await confirmAction(confirmText))) return
    if (!stillCurrent()) return
    let removed = 0
    const failures = []
    for (const id of ids) {
      if (!stillCurrent()) return
      try {
        await api.deleteSession(id)
        if (!stillCurrent()) return
        forget(id)
        removed += 1
      } catch (error) {
        if (!stillCurrent()) return
        failures.push(error?.message || id)
      }
    }
    notice.value = failures.length
      ? `已删除 ${removed} 个会话；${failures.length} 个失败：${failures.join('、')}`
      : `已删除 ${removed} 个会话（历史与用量记录保留）`
    await refresh()
  }
  const removeOne = (id) =>
    perform((stillCurrent) =>
      removeIds([id], '删除该会话？历史消息与用量记录会保留，可重新新建。', stillCurrent)
    )
  const deleteSelected = () =>
    perform((stillCurrent) =>
      removeIds(
        [...selectedIds.value],
        `删除选中的 ${selectedIds.value.length} 个会话？历史消息与用量记录会保留。`,
        stillCurrent
      )
    )
  const clearArchived = () => {
    if (!archived.value) return
    return perform((stillCurrent) =>
      removeIds(
        sessions.value.map((session) => session.id),
        `清空当前已加载的 ${sessions.value.length} 个已归档会话？${hasMore.value ? '尚未加载的会话不会删除。' : ''}历史消息与用量记录会保留。`,
        stillCurrent
      )
    )
  }
  const restore = (id) =>
    perform(async (stillCurrent) => {
      await api.updateSession(id, { status: 'active' })
      if (!stillCurrent()) return
      await refresh()
    })
  const dispose = () => {
    disposed = true
    generation++
    stopOwnerWatch()
  }
  return {
    sessions,
    selectedId,
    selectedIds,
    archived,
    busy,
    notice,
    searchText,
    hasMore,
    allSelected,
    refresh,
    loadMore,
    runSearch,
    clearSearch,
    perform,
    captureScope,
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
