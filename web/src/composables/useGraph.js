import { ref, reactive, nextTick } from 'vue'

export function useGraph(graphRef) {
  const fetching = ref(false)
  const showDetailDrawer = ref(false)
  const selectedItem = ref(null)
  const selectedItemType = ref(null)

  const graphData = reactive({
    nodes: [],
    edges: [],
    // 口径 meta（可选）：截断原因/会话过滤口径/加载时间，供洞察面板呈现统计范围
    meta: {}
  })

  const handleNodeClick = (nodeData) => {
    selectedItem.value = nodeData
    selectedItemType.value = 'node'
    showDetailDrawer.value = true
    console.log('Node clicked:', nodeData)
  }

  const handleEdgeClick = (edgeData) => {
    selectedItem.value = edgeData
    selectedItemType.value = 'edge'
    showDetailDrawer.value = true
    console.log('Edge clicked:', edgeData)
  }

  const handleCanvasClick = () => {
    showDetailDrawer.value = false
    selectedItem.value = null
    selectedItemType.value = null
    // Clear focus state on the graph component if available
    if (graphRef && graphRef.value && graphRef.value.clearFocus) {
      graphRef.value.clearFocus()
    }
  }

  const clearGraph = () => {
    graphData.nodes = []
    graphData.edges = []
    graphData.meta = {}
    handleCanvasClick()
  }

  const updateGraphData = (nodes, edges, meta = {}) => {
    graphData.nodes = nodes || []
    graphData.edges = edges || []
    graphData.meta = meta
    // Refresh graph visual after data update
    refreshGraph()
  }

  const refreshGraph = () => {
    nextTick(() => {
      if (graphRef && graphRef.value && graphRef.value.refreshGraph) {
        graphRef.value.refreshGraph()
      }
    })
  }

  return {
    fetching,
    graphData,
    showDetailDrawer,
    selectedItem,
    selectedItemType,
    handleNodeClick,
    handleEdgeClick,
    handleCanvasClick,
    clearGraph,
    updateGraphData,
    refreshGraph
  }
}
