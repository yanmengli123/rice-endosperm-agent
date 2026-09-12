import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { computePopupPosition, isValidAssistantSelection } from '@/utils/selectionPopup'

const SELECTION_CHANGE_DEBOUNCE_MS = 150

// 划词追问事件编排：mouseup 弹出工具条；选区塌陷、滚动、缩放、Escape 时收起
export function useMessageSelection({ rootEl = () => null, onFollowUp } = {}) {
  const toolbarRef = ref(null)
  const toolbarVisible = ref(false)
  const toolbarPosition = ref({ left: 0, top: 0 })
  const selectionText = ref('')

  let selectionChangeTimer = null

  const hideToolbar = () => {
    toolbarVisible.value = false
    selectionText.value = ''
  }

  const positionToolbar = (selection) => {
    const element = toolbarRef.value
    if (!element) return
    const rects = Array.from(selection.getRangeAt(0).getClientRects())
    const position = computePopupPosition(
      rects,
      { width: window.innerWidth, height: window.innerHeight },
      { width: element.offsetWidth, height: element.offsetHeight }
    )
    if (!position) {
      hideToolbar()
      return
    }
    toolbarPosition.value = position
  }

  const showToolbar = async () => {
    const selection = window.getSelection()
    const text = selection ? selection.toString() : ''
    if (!isValidAssistantSelection(selection, rootEl()) || !text.trim()) {
      hideToolbar()
      return
    }
    selectionText.value = text
    toolbarVisible.value = true
    await nextTick()
    positionToolbar(selection)
  }

  const handleMouseUp = (event) => {
    if (toolbarRef.value?.contains(event.target)) return
    showToolbar()
  }

  const handleSelectionChange = () => {
    if (selectionChangeTimer) {
      clearTimeout(selectionChangeTimer)
    }
    selectionChangeTimer = setTimeout(() => {
      if (!toolbarVisible.value) return
      const selection = window.getSelection()
      if (!isValidAssistantSelection(selection, rootEl()) || !selection.toString().trim()) {
        hideToolbar()
      }
    }, SELECTION_CHANGE_DEBOUNCE_MS)
  }

  const handleDismiss = () => {
    if (toolbarVisible.value) {
      hideToolbar()
    }
  }

  const handleKeydown = (event) => {
    if (event.key === 'Escape') {
      handleDismiss()
    }
  }

  // 先收起再回调：编辑器聚焦会触发 selectionchange，避免与收起逻辑竞态
  const followUpSelection = () => {
    const text = selectionText.value
    hideToolbar()
    if (text && typeof onFollowUp === 'function') {
      onFollowUp(text)
    }
  }

  onMounted(() => {
    document.addEventListener('mouseup', handleMouseUp, true)
    document.addEventListener('selectionchange', handleSelectionChange)
    document.addEventListener('scroll', handleDismiss, true)
    window.addEventListener('resize', handleDismiss)
    window.addEventListener('keydown', handleKeydown)
  })

  onBeforeUnmount(() => {
    document.removeEventListener('mouseup', handleMouseUp, true)
    document.removeEventListener('selectionchange', handleSelectionChange)
    document.removeEventListener('scroll', handleDismiss, true)
    window.removeEventListener('resize', handleDismiss)
    window.removeEventListener('keydown', handleKeydown)
    if (selectionChangeTimer) {
      clearTimeout(selectionChangeTimer)
      selectionChangeTimer = null
    }
  })

  return {
    toolbarRef,
    toolbarVisible,
    toolbarPosition,
    selectionText,
    followUpSelection
  }
}
