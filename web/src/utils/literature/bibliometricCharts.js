/**
 * 文献计量图表 option 构建（纯函数，不 import echarts）。
 * 对齐 utils/graph/chartOptions.js 先例：option 构建与 echarts 实例解耦，
 * theme 由组件层从 CSS 变量注入（裸 node 可测）。
 *
 * theme: { colors: string[], textColor, splitLineColor, axisLineColor }
 * 裸 node 测试：node src/utils/literature/__tests__/bibliometricCharts.spec.js
 */

export const LANGUAGE_LABELS = {
  unknown: '未标注',
  en: '英文',
  zh: '中文',
  ja: '日文',
  ko: '韩文',
  fr: '法文',
  de: '德文',
  ru: '俄文'
}
export const KIND_LABELS = {
  paper: '论文',
  chapter: '书章',
  book: '图书',
  thesis: '学位论文',
  patent: '专利',
  report: '报告'
}

export const languageLabel = (key) => LANGUAGE_LABELS[key] || key || '未标注'
export const kindLabel = (key) => KIND_LABELS[key] || key || '未知'

export const truncateText = (text, maxLength = 24) => {
  const value = String(text || '')
  return value.length > maxLength ? `${value.slice(0, maxLength)}…` : value
}

/** 年度发文趋势：按语言堆叠柱（yearly 来自 /metrics/bibliometrics，已补零）。 */
export function buildYearlyTrendOption(yearly, theme) {
  const rows = Array.isArray(yearly) ? yearly : []
  const languages = [...new Set(rows.flatMap((item) => Object.keys(item.by_language || {})))].sort()
  return {
    color: theme.colors,
    tooltip: { trigger: 'axis', renderMode: 'richText' },
    legend: { textStyle: { color: theme.textColor } },
    grid: { left: 48, right: 16, top: 34, bottom: 28 },
    xAxis: {
      type: 'category',
      data: rows.map((item) => item.year),
      axisLabel: { color: theme.textColor },
      axisLine: { lineStyle: { color: theme.axisLineColor } }
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: theme.textColor },
      splitLine: { lineStyle: { color: theme.splitLineColor } }
    },
    series: languages.map((language) => ({
      name: languageLabel(language),
      type: 'bar',
      stack: 'total',
      emphasis: { focus: 'series' },
      data: rows.map((item) => item.by_language?.[language] || 0)
    }))
  }
}

/** TopN 横向条形（期刊/机构共用：倒序使榜首在顶部）。 */
export function buildTopBarsOption(
  items,
  theme,
  { nameKey = 'name', valueKey = 'count', color } = {}
) {
  const reversed = [...(items || [])].reverse()
  return {
    color: [color || theme.colors[0]],
    tooltip: { trigger: 'axis', renderMode: 'richText', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 44, top: 8, bottom: 8, containLabel: true },
    xAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: theme.textColor },
      splitLine: { lineStyle: { color: theme.splitLineColor } }
    },
    yAxis: {
      type: 'category',
      data: reversed.map((item) => item[nameKey]),
      axisLabel: { color: theme.textColor, formatter: (value) => truncateText(value) },
      axisLine: { lineStyle: { color: theme.axisLineColor } }
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 18,
        data: reversed.map((item) => item[valueKey]),
        label: { show: true, position: 'right', color: theme.textColor }
      }
    ]
  }
}

/** 分布环形图（by_kind / by_language 等计数字典；labelOf 做显示名映射）。 */
export function buildDistributionOption(distribution, theme, labelOf = (key) => key) {
  const entries = Object.entries(distribution || {}).sort((a, b) => b[1] - a[1])
  return {
    color: theme.colors,
    tooltip: { trigger: 'item', renderMode: 'richText' },
    legend: { bottom: 0, type: 'scroll', textStyle: { color: theme.textColor } },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        label: { show: false },
        data: entries.map(([name, value]) => ({ name: labelOf(name), value }))
      }
    ]
  }
}

/** 共现网络（/metrics/networks）：节点大小按频次占比（0→minSize，max→maxSize），边宽按共现次数。 */
export function buildNetworkOption(network, theme, { minSize = 8, maxSize = 44 } = {}) {
  const nodes = network?.nodes || []
  const links = network?.links || []
  const maxWeight = Math.max(...nodes.map((node) => node.weight), 1)
  const sizeOf = (weight) => Math.round(minSize + (weight / maxWeight) * (maxSize - minSize))
  return {
    color: theme.colors,
    tooltip: {
      show: true,
      renderMode: 'richText',
      formatter: (params) =>
        params.dataType === 'edge'
          ? `${params.data.source} — ${params.data.target}：共现 ${params.data.value} 次`
          : `${params.name}：出现 ${params.data.rawWeight} 次`
    },
    animationDuration: 400,
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: { repulsion: 130, edgeLength: [40, 130], gravity: 0.08 },
        label: { show: true, color: theme.textColor, fontSize: 10 },
        lineStyle: { color: theme.splitLineColor, curveness: 0.08 },
        emphasis: { focus: 'adjacency' },
        data: nodes.map((node, index) => ({
          id: node.id,
          name: node.label,
          rawWeight: node.weight,
          symbolSize: sizeOf(node.weight),
          itemStyle: { color: theme.colors[index % theme.colors.length] }
        })),
        links: links.map((link) => ({
          source: link.source,
          target: link.target,
          value: link.weight,
          lineStyle: { width: Math.min(4, 1 + link.weight * 0.3) }
        }))
      }
    ]
  }
}
