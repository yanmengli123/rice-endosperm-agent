/**
 * 图谱洞察三视图（弦图/桑基流向/热力矩阵）的 ECharts option 构建器。
 *
 * 纯函数分层：只依赖聚合数据（chartAggregations）与注入的主题参数，不读 DOM、
 * 不 import echarts/tooltip 文案模块——主题参数（颜色/透明度）由面板解析 CSS
 * 变量后传入，保证可裸 node 测试与暗色适配的一致入口。
 *
 * 既有约束（docs/vibe/2026-10-05-graph-insight-contract）：
 * - ECharts 6 弦带/桑基连线填色仅解析 source/target/gradient，不直接传颜色字符串；
 * - 弧段大小只表示已绘制关联量，不人为抬高弱类型角度；
 * - 弦带/连线透明度随主题注入（暗色底需更高不透明度）。
 */

export function buildChordOption(data, theme) {
  const adaptColor = (color) => (theme.isDark ? theme.lighten(color) : color)
  const nodes = data.nodes
    .filter((n) => n.drawnTotal > 0)
    .map((n) => ({ ...n, itemStyle: { ...n.itemStyle, color: adaptColor(n.itemStyle.color) } }))
  const edges = data.links.map((link) => ({
    ...link,
    lineStyle: { color: 'source', opacity: theme.ribbonOpacity }
  }))
  return {
    animation: !theme.largeScale,
    animationDuration: 400,
    tooltip: theme.tooltip,
    series: [
      {
        type: 'chord',
        // 间隔有总量上限；不抬高弱类型角度，以保留真实计数比例。
        padAngle: Math.min(2, 60 / Math.max(nodes.length, 1)),
        minAngle: 0,
        data: nodes,
        edges,
        // 不用 labelLayout.hideOverlap：避让算法在部分标签组合下会无限震荡。
        // 超过 12 类型时隐藏外圈标签（识别走 tooltip 与下方关联列表）。
        label: {
          position: 'outside',
          fontSize: 11,
          color: theme.labelColor,
          show: nodes.length <= 12
        },
        itemStyle: { borderWidth: 1, borderColor: theme.arcBorderColor },
        emphasis: { focus: 'adjacency' }
      }
    ]
  }
}

export function buildSankeyOption(data, theme) {
  const adaptColor = (color) => (theme.isDark ? theme.lighten(color) : color)
  // 二部投影：ECharts 桑基是 DAG 布局，开放词表的互指/环路类型对会直接抛错
  // （Sankey is a DAG, the original data has cycle!）。每个类型在左右各出一个
  // 节点（独立的展示 ID 与业务类型名分离），所有流严格左→右——环路在构造上
  // 不可能，且 A→B 与 B→A 各有独立通道（互指关系不再被合并或丢弃）。
  const nodeById = new Map(data.nodes.map((n) => [n.name, n]))
  const nodes = []
  const sideNodes = new Map()
  const ensureSide = (name, side) => {
    const meta = nodeById.get(name)
    if (!meta) return
    const id = JSON.stringify([side, name])
    if (!sideNodes.has(id)) {
      const node = {
        ...meta,
        name: id,
        typeName: name,
        side,
        total: side === 'source' ? meta.outgoing : meta.incoming,
        drawnTotal: 0,
        itemStyle: { color: adaptColor(meta.itemStyle.color) },
        label: { position: side === 'target' ? 'right' : 'left' }
      }
      sideNodes.set(id, node)
      nodes.push(node)
    }
    return sideNodes.get(id)
  }
  const links = []
  for (const link of data.directedLinks) {
    if (!nodeById.has(link.source) || !nodeById.has(link.target)) continue
    const source = ensureSide(link.source, 'source')
    const target = ensureSide(link.target, 'target')
    source.drawnTotal += link.value
    target.drawnTotal += link.value
    links.push({
      ...link,
      sourceType: link.source,
      targetType: link.target,
      source: source.name,
      target: target.name
    })
  }
  const sideCount = Math.max(
    nodes.filter((node) => node.side === 'source').length,
    nodes.filter((node) => node.side === 'target').length
  )
  return {
    animation: !theme.largeScale,
    animationDuration: 400,
    tooltip: theme.tooltip,
    series: [
      {
        type: 'sankey',
        data: nodes,
        links,
        // 大规模档（全量数百类型）：迭代 32→3 并关闭动画——迭代松弛与
        // 逐帧全量重绘是同步长任务的主要来源（实测 746 节点阻塞 7.5s+）
        layoutIterations: theme.largeScale ? 3 : 32,
        nodeAlign: 'justify',
        // 密集侧的间距总和不超过绘图区的一半，避免 50 类型时出现负节点高度。
        nodeGap: Math.min(8, 160 / Math.max(sideCount - 1, 1)),
        lineStyle: { color: 'source', opacity: theme.sankeyLinkOpacity, curveness: 0.5 },
        label: {
          show: sideCount <= 24,
          color: theme.labelColor,
          fontSize: 11,
          formatter: (p) => {
            const name = p.data.typeName
            return name.length > 8 ? name.slice(0, 8) + '…' : name
          }
        },
        itemStyle: { borderWidth: 1, borderColor: theme.arcBorderColor },
        emphasis: { focus: 'adjacency' },
        left: 64,
        right: 64,
        top: 6,
        bottom: 6
      }
    ]
  }
}

export function buildHeatmapOption(data, theme) {
  const categories = data.nodes.map((n) => n.name)
  const indexOf = new Map(categories.map((name, index) => [name, index]))
  const cells = []
  for (const link of data.directedLinks) {
    const col = indexOf.get(link.target)
    const row = indexOf.get(link.source)
    if (col == null || row == null) continue
    cells.push({ ...link, value: [col, row, link.value] })
  }
  const maxValue = cells.reduce((max, cell) => Math.max(max, cell.value[2]), 0)
  const windowEnd = Math.min(categories.length, 24) - 1
  const zoom = categories.length > 24
  return {
    animation: !theme.largeScale,
    animationDuration: 400,
    tooltip: theme.tooltip,
    grid: { left: 76, right: zoom ? 70 : 46, top: 24, bottom: zoom ? 96 : 66 },
    dataZoom: zoom
      ? [
          {
            type: 'slider',
            xAxisIndex: 0,
            filterMode: 'none',
            startValue: 0,
            endValue: windowEnd,
            bottom: 8,
            height: 12,
            showDetail: false,
            brushSelect: false
          },
          {
            type: 'slider',
            yAxisIndex: 0,
            filterMode: 'none',
            startValue: 0,
            endValue: windowEnd,
            right: 44,
            width: 12,
            showDetail: false,
            brushSelect: false
          },
          { type: 'inside', xAxisIndex: 0, filterMode: 'none', startValue: 0, endValue: windowEnd },
          { type: 'inside', yAxisIndex: 0, filterMode: 'none', startValue: 0, endValue: windowEnd }
        ]
      : [],
    xAxis: {
      type: 'category',
      name: '目标',
      data: categories,
      axisLabel: { rotate: 45, fontSize: 10, color: theme.labelColor },
      splitArea: { show: false }
    },
    yAxis: {
      type: 'category',
      inverse: true,
      name: '源',
      data: categories,
      axisLabel: { fontSize: 10, color: theme.labelColor },
      splitArea: { show: false }
    },
    visualMap: {
      type: 'continuous',
      min: 0,
      max: Math.max(maxValue, 1),
      orient: 'vertical',
      right: 0,
      top: 'center',
      itemHeight: 90,
      calculable: false,
      inRange: { color: theme.heatRamp },
      textStyle: { color: theme.labelColor, fontSize: 10 }
    },
    series: [
      {
        type: 'heatmap',
        data: cells,
        itemStyle: { borderColor: theme.arcBorderColor, borderWidth: 1 },
        emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.4)' } }
      }
    ]
  }
}
