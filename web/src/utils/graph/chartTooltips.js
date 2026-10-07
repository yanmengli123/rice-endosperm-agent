/**
 * 图谱洞察图表 tooltip 纯文本层。
 *
 * LLM/导入的类型与谓词只能作为文字进入 tooltip，绝不作为 HTML —— 动态值一律
 * encodeHTML 转义（本地实现，不 import echarts，保持可裸 node 测试）。
 */

export function encodeHTML(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function formatChordTooltip(params) {
  const text = encodeHTML
  const d = params.data || {}
  if (params.dataType === 'node') {
    const lines = [
      `<b>${text(d.name)}</b>`,
      `已绘制关联：${text(d.drawnTotal)} 条`,
      `当前结果跨类型关联：${text(d.total)} 条`
    ]
    if (d.selfLoops > 0) lines.push(`类型内部关系：${text(d.selfLoops)} 条（见内部关系表）`)
    return lines.join('<br/>')
  }
  if (params.dataType === 'edge') {
    const lines = [`<b>${text(d.source)} ↔ ${text(d.target)}</b>：${text(d.value)} 条去重关系`]
    for (const row of (d.semantics || []).slice(0, 3)) {
      lines.push(
        `${text(row.source)} → ${text(row.target)} · ${text(row.type)}：${text(row.count)}`
      )
    }
    lines.push('点击查看全部方向、谓词与原文证据')
    return lines.join('<br/>')
  }
  return text(params.name)
}

export function formatSankeyTooltip(params) {
  const text = encodeHTML
  const d = params.data || {}
  if (params.dataType === 'node') {
    const side = d.side === 'target' ? '入向' : '出向'
    return `<b>${text(d.typeName)}</b><br/>${side}关系（含类型内部）：${text(d.total)} 条（已绘制 ${text(d.drawnTotal)}）`
  }
  if (params.dataType === 'edge') {
    const lines = [
      `<b>${text(d.sourceType)} → ${text(d.targetType)}</b>：${text(d.value)} 条去重关系`
    ]
    for (const row of (d.relationTypes || []).slice(0, 3)) {
      lines.push(`${text(row.type)}：${text(row.count)}`)
    }
    if ((d.relationTypes || []).length > 3) lines.push(`等 ${d.relationTypes.length} 种谓词`)
    lines.push('点击查看该方向全部谓词与原文证据')
    return lines.join('<br/>')
  }
  return text(params.name)
}

/** 热力图 formatter 工厂：单元格坐标 → 类目名（行=源、列=目标），对角线标注自环。 */
export function makeHeatmapTooltip(categories) {
  const text = encodeHTML
  const names = categories || []
  return (params) => {
    const [colIdx, rowIdx, value] = params.value || []
    const col = names[colIdx]
    const row = names[rowIdx]
    if (col == null || row == null) return text(params.name)
    const internal = colIdx === rowIdx
    const lines = [
      internal
        ? `<b>${text(row)}</b><br/>类型内部关系：${text(value)} 条（对角线，弦图不计入弦带）<br/>点击查看内部关系明细`
        : `<b>${text(row)} → ${text(col)}</b>：${text(value)} 条去重关系<br/>点击查看该方向的谓词构成与证据`
    ]
    for (const predicate of (params.data?.relationTypes || []).slice(0, 3)) {
      lines.push(`${text(predicate.type)}：${text(predicate.count)}`)
    }
    return lines.join('<br/>')
  }
}
