// 文献目录重复检测：同 DOI（优先）或同规范化标题视为疑似重复。
// 仅做展示级标记，不自动合并——合并/回收是用户决策，去重入库是后端导入契约的事。

const normalizeTitle = (title) =>
  String(title || '')
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '')

const duplicateKey = (paper) => {
  if (paper?.doi) return `doi:${String(paper.doi).trim().toLowerCase()}`
  const title = normalizeTitle(paper?.title)
  return title ? `title:${title}` : null
}

/**
 * 统计当前页文献的疑似重复数。
 * @param {Array<{rid: string, doi?: string, title?: string}>} papers
 * @returns {Map<string, number>} rid → 同键条目总数（1 即无重复；无 DOI 且标题
 *   规范化后为空的条目不参与检测，恒记 1）
 */
export function duplicateCounts(papers) {
  const keyTotals = new Map()
  for (const paper of papers || []) {
    const key = duplicateKey(paper)
    if (!key) continue
    keyTotals.set(key, (keyTotals.get(key) || 0) + 1)
  }
  const byRid = new Map()
  for (const paper of papers || []) {
    const key = duplicateKey(paper)
    byRid.set(paper.rid, key ? keyTotals.get(key) : 1)
  }
  return byRid
}
