/**
 * ARIA tablist 键盘导航（WAI-ARIA tabs 模式）：
 * roving tabindex + 左右方向键（含 Home/End）激活，tab 键只在当前 tab 上停留。
 * 与 `role="tab"` + `aria-selected` + 面板侧 `role="tabpanel"` 搭配使用；
 * 面板容器同时给 `:id` 与 tab 的 `aria-controls`。
 * 纯 DOM 事件转发，无状态注入——调用方只把 onTabKeydown 绑到每个 tab 按钮上。
 * @param {(key: string) => (boolean | void | Promise<boolean | void>)} activate
 * 选中某 tab 的回调（与点击同路径），返回 false 表示确认被取消。
 * @param {() => string[]} orderedKeys 与渲染顺序一致的 tab key 列表
 */
export function useTablistKeyboard(activate, orderedKeys) {
  let pending = false
  const onTabKeydown = async (event) => {
    // Capture before awaiting: DOM Event.currentTarget is cleared after dispatch.
    const origin = event.currentTarget
    const tablist = origin?.closest('[role="tablist"]')
    if (!tablist) return
    const vertical = tablist.getAttribute('aria-orientation') === 'vertical'
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"][data-tab-key]'))
    const keys = orderedKeys().filter((key) =>
      tabs.some(
        (tab) =>
          tab.dataset.tabKey === key &&
          !tab.disabled &&
          tab.getAttribute('aria-disabled') !== 'true'
      )
    )
    const current = keys.indexOf(origin.dataset.tabKey)
    if (current < 0 || !keys.length) return
    let next
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        if ((event.key === 'ArrowDown') !== vertical) return
        next = (current + 1) % keys.length
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        if ((event.key === 'ArrowUp') !== vertical) return
        next = (current - 1 + keys.length) % keys.length
        break
      case 'Home':
        next = 0
        break
      case 'End':
        next = keys.length - 1
        break
      default:
        return
    }
    event.preventDefault()
    if (pending) return
    pending = true
    try {
      if ((await activate(keys[next])) === false) return
      if (tablist.isConnected === false) return
      // Compare dataset values instead of interpolating keys into CSS selectors.
      tabs.find((tab) => tab.dataset.tabKey === keys[next])?.focus()
    } finally {
      pending = false
    }
  }
  return { onTabKeydown }
}
