// Chat shell geometry. The route may force the sidebar collapsed (for example the
// literature workbench owns the full desktop width); that must be derived, never
// written back into the persisted user preference, otherwise leaving the route
// (or a reload) would silently overwrite the preference.
export function isSidebarCompactRoute(routeMeta) {
  return routeMeta?.compactShell === true
}

/**
 * Effective collapsed state = user preference OR route requirement.
 * Pure so it can be unit-tested without mounting the layout.
 */
export function resolveSidebarCollapsed(preference, routeMeta) {
  return preference === true || isSidebarCompactRoute(routeMeta)
}

// Literature workspace chrome. Mirrors the PaperAgent reading-desk layout:
// a pinned 4-drawer sidebar plus a main area, both resizable by pointer and
// keyboard. Widths are pure functions so they stay unit-testable.
export const LIT_SIDEBAR_WIDTH_MIN = 260
export const LIT_SIDEBAR_WIDTH_MAX = 560
export const LIT_SIDEBAR_WIDTH_DEFAULT = 320

export const clampLitSidebarWidth = (value) => {
  const width = Number(value)
  if (!Number.isFinite(width)) return LIT_SIDEBAR_WIDTH_DEFAULT
  return Math.min(LIT_SIDEBAR_WIDTH_MAX, Math.max(LIT_SIDEBAR_WIDTH_MIN, Math.round(width)))
}

const DRAWERS = ['sessions', 'papers', 'workbench', 'diary']

export const normalizeLitChrome = (raw) => ({
  drawer: DRAWERS.includes(raw?.drawer) ? raw.drawer : 'papers',
  sidebarWidth: clampLitSidebarWidth(raw?.sidebarWidth),
  pinned: raw?.pinned !== false,
  readMode: raw?.readMode === true,
  eyeCare: raw?.eyeCare === true,
  // 分栏问答的会话/文献左右位置互换偏好（true = 会话在右、文献在左）
  splitSwapped: raw?.splitSwapped === true
})

export const readLitChrome = (storage, uid) => {
  try {
    return normalizeLitChrome(uid ? JSON.parse(storage.getItem(`yuxi:literature:chrome:${uid}`)) : null)
  } catch {
    return normalizeLitChrome(null)
  }
}

export const writeLitChrome = (storage, uid, value) => {
  if (!uid) return
  try {
    storage.setItem(`yuxi:literature:chrome:${uid}`, JSON.stringify(normalizeLitChrome(value)))
  } catch {
    /* browser storage can be denied; layout preferences never block reading */
  }
}
