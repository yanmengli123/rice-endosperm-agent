// Derived analytical charts; no references, claims or evidence are fabricated here.
export const TOPIC_CHARTS = [
  ['overview', '主题总览'],
  ['keywords', '主题关键词'],
  ['cloud', '主题词云'],
  ['hierarchy', '主题层级'],
  ['documents', '文献语义分布'],
  ['heatmap', '主题关键词热力图'],
  ['ranks', '关键词秩'],
  ['trends', '年度主题趋势'],
  ['groups', '主题分组构成'],
  ['similarity', '主题相似度'],
  ['network', '主题关系网络'],
  ['pareto', '主题规模分布'],
  ['noise', '归类与离群'],
  ['quality', '参数质量诊断'],
  ['sankey', '数据库到主题构成']
]

export function buildTopicChart(kind, data, theme, selectedTopic, settings = {}) {
  const topics = [...(data.topics || [])].sort((a, b) => a.topic - b.topic)
  const assignments = data.assignments || []
  const metadata = data.metadata || {}
  const names = new Map(topics.map((t) => [t.topic, `T${t.topic} ${t.label}`]))
  names.set(-1, '离群记录')
  const label = (id) => names.get(id) || `T${id}`
  const chosen =
    selectedTopic === -1 ? null : topics.find((t) => t.topic === selectedTopic) || topics[0]
  const colors = theme.colors
  const base = {
    color: colors,
    animation: false,
    textStyle: { color: theme.textColor },
    tooltip: { trigger: 'item', renderMode: 'richText' },
    grid: { left: 75, right: 35, top: 35, bottom: 75, containLabel: true },
    xAxis: { type: 'category', axisLabel: { color: theme.textColor } },
    yAxis: {
      type: 'value',
      axisLabel: { color: theme.textColor },
      splitLine: { lineStyle: { color: theme.splitLineColor } }
    }
  }
  if (kind === 'keywords' || kind === 'ranks') {
    const words = chosen?.keywords || []
    return {
      ...base,
      xAxis: {
        ...base.xAxis,
        data: words.map((w) => w.term),
        axisLabel: { rotate: 45, color: theme.textColor }
      },
      series: [{ type: kind === 'ranks' ? 'line' : 'bar', data: words.map((w) => w.weight) }],
      yAxis: { ...base.yAxis, name: 'c-TF-IDF 权重' }
    }
  }
  if (kind === 'cloud')
    return {
      ...base,
      xAxis: undefined,
      yAxis: undefined,
      graphic: placeWords(
        settings.wordWeight === 'frequency'
          ? (chosen?.word_frequencies || []).map(([term, weight]) => ({ term, weight }))
          : chosen?.keywords || [],
        colors
      )
    }
  if (kind === 'documents' || kind === 'overview') {
    const points =
      kind === 'documents'
        ? assignments.map((a) => ({
            value: [a.x, a.y],
            topic: a.topic,
            name: metadata[a.document_key]?.title || a.document_key,
            documentKey: a.document_key
          }))
        : topics.map((t) => {
            const members = assignments.filter((a) => a.topic === t.topic)
            return {
              name: label(t.topic),
              topic: t.topic,
              value: [
                members.reduce((n, a) => n + a.x, 0) / Math.max(1, members.length),
                members.reduce((n, a) => n + a.y, 0) / Math.max(1, members.length),
                t.count
              ]
            }
          })
    return {
      ...base,
      xAxis: { type: 'value', name: 'UMAP 投影 1' },
      yAxis: { ...base.yAxis, name: 'UMAP 投影 2' },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0 },
        { type: 'inside', yAxisIndex: 0 }
      ],
      series: [
        {
          type: 'scatter',
          // Large scatter mode discards item colours and per-document click targets.
          large: false,
          progressive: 3000,
          symbolSize:
            kind === 'documents' ? 5 : (v) => Math.max(10, Math.min(90, 2 * Math.sqrt(v[2]))),
          data: points.map((p) => ({
            ...p,
            itemStyle: {
              color: p.topic < 0 ? theme.splitLineColor : colors[p.topic % colors.length]
            }
          }))
        }
      ]
    }
  }
  if (kind === 'hierarchy') {
    // Linkage leaf indices follow sorted topic IDs, not the file's summary order.
    const tree = [...topics]
      .sort((a, b) => a.topic - b.topic)
      .map((t) => ({ name: label(t.topic), topic: t.topic, value: t.count }))
    for (const row of data.hierarchy || [])
      tree.push({
        name: `距离 ${Number(row[2]).toFixed(3)}`,
        children: [tree[row[0]], tree[row[1]]].filter(Boolean)
      })
    return {
      ...base,
      xAxis: undefined,
      yAxis: undefined,
      series: [
        {
          type: 'tree',
          data: tree.length ? [tree.at(-1)] : [],
          initialTreeDepth: 3,
          roam: true,
          label: { color: theme.textColor },
          leaves: { label: { color: theme.textColor } }
        }
      ]
    }
  }
  if (kind === 'heatmap') {
    const words = [
      ...new Set(topics.flatMap((t) => (t.keywords || []).slice(0, 5).map((w) => w.term)))
    ].slice(0, 150)
    const values = topics.flatMap((t, y) =>
      words.map((w, x) => [x, y, t.keywords.find((k) => k.term === w)?.weight || 0])
    )
    return {
      ...base,
      xAxis: { ...base.xAxis, data: words, axisLabel: { rotate: 60, color: theme.textColor } },
      yAxis: { type: 'category', data: topics.map((t) => `T${t.topic}`) },
      visualMap: {
        min: 0,
        max: Math.max(0.001, ...values.map((v) => v[2])),
        calculable: true,
        orient: 'horizontal',
        bottom: 0,
        inRange: { color: [theme.splitLineColor, colors[0]] }
      },
      series: [{ type: 'heatmap', data: values }]
    }
  }
  if (kind === 'similarity') {
    const ids = topics.map((t) => t.topic),
      positions = new Map(ids.map((id, i) => [id, i]))
    const pairs = (data.similarity || []).flatMap((p) => [
      [positions.get(p.a), positions.get(p.b), p.cosine],
      [positions.get(p.b), positions.get(p.a), p.cosine]
    ])
    return {
      ...base,
      xAxis: { type: 'category', data: ids.map((id) => `T${id}`) },
      yAxis: { type: 'category', data: ids.map((id) => `T${id}`) },
      visualMap: { min: -1, max: 1, calculable: true, orient: 'horizontal', bottom: 0 },
      series: [{ type: 'heatmap', data: [...pairs, ...ids.map((_, i) => [i, i, 1])] }]
    }
  }
  if (kind === 'trends') {
    const eligible = (data.yearly || []).filter(
      (r) => !data.analysis_year || r.year <= data.analysis_year
    )
    const years = [...new Set(eligible.map((r) => r.year))].sort((a, b) => a - b)
    const rows = new Map((data.yearly || []).map((r) => [`${r.year}:${r.topic}`, r]))
    return {
      ...base,
      tooltip: { trigger: 'axis', renderMode: 'richText' },
      legend: { type: 'scroll', textStyle: { color: theme.textColor } },
      xAxis: { ...base.xAxis, data: years },
      yAxis: { ...base.yAxis, name: '当年全部记录占比 (%)' },
      series: [...topics.map((t) => t.topic), -1].map((id) => ({
        type: 'line',
        name: label(id),
        showSymbol: false,
        data: years.map((y) => {
          const r = rows.get(`${y}:${id}`)
          return r ? (100 * r.count) / r.denominator : 0
        })
      }))
    }
  }
  if (kind === 'network')
    return {
      ...base,
      xAxis: undefined,
      yAxis: undefined,
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          data: topics.map((t) => ({
            id: String(t.topic),
            name: label(t.topic),
            topic: t.topic,
            value: t.count,
            symbolSize: Math.max(10, Math.min(70, 2 * Math.sqrt(t.count)))
          })),
          links: (data.similarity || [])
            .filter((p) => p.cosine >= 0.7)
            .map((p) => ({ source: String(p.a), target: String(p.b), value: p.cosine })),
          force: { repulsion: 250 },
          label: { show: true, color: theme.textColor, formatter: '{b}' }
        }
      ]
    }
  if (kind === 'pareto') {
    const sorted = [...topics].sort((a, b) => b.count - a.count),
      total = assignments.length
    let cumulative = 0
    return {
      ...base,
      xAxis: { ...base.xAxis, data: sorted.map((t) => `T${t.topic}`) },
      yAxis: [base.yAxis, { type: 'value', min: 0, max: 100, name: '累计占全部输入 (%)' }],
      series: [
        { type: 'bar', data: sorted.map((t) => ({ value: t.count, topic: t.topic })) },
        {
          type: 'line',
          yAxisIndex: 1,
          data: sorted.map((t) => {
            cumulative += t.count
            return total ? (cumulative / total) * 100 : 0
          })
        }
      ]
    }
  }
  if (kind === 'noise')
    return {
      ...base,
      xAxis: undefined,
      yAxis: undefined,
      series: [
        {
          type: 'pie',
          radius: ['35%', '65%'],
          data: [
            { name: '已归类', value: assignments.filter((a) => a.topic >= 0).length },
            { name: '离群 (-1)', topic: -1, value: assignments.filter((a) => a.topic < 0).length }
          ]
        }
      ]
    }
  if (kind === 'quality') {
    const candidates = data.candidates || [],
      keys = ['coverage', 'npmi', 'silhouette', 'diversity']
    const field = (r, k) =>
      r[k] ??
      r[
        {
          npmi: 'mean_npmi_top10',
          silhouette: 'semantic_silhouette_sample1500',
          diversity: 'topic_diversity_top10'
        }[k]
      ] ??
      null
    return {
      ...base,
      legend: { data: keys, textStyle: { color: theme.textColor } },
      xAxis: { ...base.xAxis, data: candidates.map((r) => String(r.min_cluster_size)) },
      yAxis: { ...base.yAxis, min: -1, max: 1 },
      series: keys.map((k) => ({ name: k, type: 'bar', data: candidates.map((r) => field(r, k)) }))
    }
  }
  const groups = new Map()
  const dimension = kind === 'sankey' ? 'source' : settings.groupBy || 'source'
  const groupValues = (a) => {
    const d = metadata[a.document_key] || {}
    const value =
      dimension === 'country'
        ? d.countries
        : dimension === 'affiliation'
          ? d.affiliations
          : d[dimension]
    const values = Array.isArray(value) ? value : [value]
    const names = [
      ...new Set(values.filter((v) => typeof v === 'string' && v.trim()).map((v) => v.trim()))
    ]
    return names.length ? names : ['unknown']
  }
  const frequency = new Map()
  for (const a of assignments)
    for (const value of groupValues(a)) frequency.set(value, (frequency.get(value) || 0) + 1)
  const top = new Set(
    [...frequency]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 20)
      .map(([value]) => value)
  )
  for (const a of assignments) {
    for (const source of new Set(
      groupValues(a).map((value) => (top.has(value) ? value : '其他（Top20 之外）'))
    )) {
      if (!groups.has(source)) groups.set(source, new Map())
      const values = groups.get(source)
      values.set(a.topic, (values.get(a.topic) || 0) + 1)
    }
  }
  const sources = [...groups.keys()]
  if (kind === 'sankey')
    return {
      ...base,
      xAxis: undefined,
      yAxis: undefined,
      series: [
        {
          type: 'sankey',
          data: [
            ...sources.map((s) => ({ name: `来源 ${s}` })),
            ...[...new Set(assignments.map((a) => a.topic))].map((id) => ({
              name: label(id),
              topic: id
            }))
          ],
          links: sources.flatMap((s) =>
            [...groups.get(s)].map(([id, value]) => ({
              source: `来源 ${s}`,
              target: label(id),
              value
            }))
          ),
          label: { color: theme.textColor }
        }
      ]
    }
  return {
    ...base,
    xAxis: { ...base.xAxis, data: sources },
    yAxis: { ...base.yAxis, name: '来源内占比 (%)', max: 100 },
    series: [...topics.map((t) => t.topic), -1].map((id) => ({
      type: 'bar',
      stack: 'source',
      name: label(id),
      data: sources.map((s) => {
        const counts = groups.get(s)
        return (100 * (counts.get(id) || 0)) / [...counts.values()].reduce((a, b) => a + b, 0)
      })
    }))
  }
}

function placeWords(words, colors) {
  const max = Math.max(0.00001, ...words.map((w) => w.weight)),
    placed = [],
    result = []
  for (const [index, word] of words.slice(0, 60).entries()) {
    const size = 12 + Math.sqrt(word.weight / max) * 32
    const width =
      [...word.term].reduce((n, c) => n + (/[\u3400-\u9fff]/.test(c) ? size : size * 0.65), 0) + 10
    const height = size + 10
    for (let attempt = 0; attempt < 2000; attempt++) {
      const angle = attempt * 0.55,
        radius = 4 * Math.sqrt(attempt)
      const x = 450 + radius * Math.cos(angle) * 2 - width / 2,
        y = 250 + radius * Math.sin(angle) - height / 2
      if (
        x < 0 ||
        y < 0 ||
        x + width > 900 ||
        y + height > 500 ||
        placed.some(
          (b) => x < b.x + b.width && x + width > b.x && y < b.y + b.height && y + height > b.y
        )
      )
        continue
      placed.push({ x, y, width, height })
      result.push({
        type: 'text',
        x,
        y,
        style: { text: word.term, fontSize: size, fill: colors[index % colors.length] }
      })
      break
    }
  }
  return [{ type: 'group', left: 'center', top: 'middle', children: result }]
}
